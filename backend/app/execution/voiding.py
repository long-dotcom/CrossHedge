"""异常对冲组的安全作废归档。

作废只终止没有外部敞口的脏状态，绝不删除 Intent、订单、成交或事件。只要存在
真实成交、结果未知订单或尚未回平的真实探针，就必须先走对账/恢复流程。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import (
    ExecutionIntent,
    ExecutionLeg,
    ExecutionOutbox,
    Fill,
    HedgeGroup,
    HedgeGroupEvent,
    Order,
    ProbeRun,
    VenueOrder,
)


SAFE_PROBE_STATUSES = {"FLAT", "FAILED_NO_EXPOSURE", "FAILED_SAMPLE_INVALID"}
SAFE_EMPTY_VENUE_ORDER_STATUSES = {"INITIALIZED", "CREATED", "REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}
SAFE_EMPTY_LEGACY_ORDER_STATUSES = {"new", "initialized", "rejected", "failed", "canceled", "cancelled", "expired"}
TERMINAL_LEG_STATUSES = {"FILLED", "REJECTED", "FAILED", "CANCELED", "CANCELLED"}
TERMINAL_INTENT_STATUSES = {"COMPLETED", "FAILED", "VOIDED"}


@dataclass(frozen=True)
class VoidEligibility:
    allowed: bool
    reason: str


def void_eligibility(db: Session, group: HedgeGroup) -> VoidEligibility:
    """仅依据持久化执行事实判断是否允许作废，不在 HTTP 线程访问交易场所。"""
    if str(group.status or "").lower() == "voided":
        return VoidEligibility(False, "该对冲组已经作废归档")

    probes = db.query(ProbeRun).filter(ProbeRun.hedge_group_id == group.id).all()
    unsafe_probes = [run for run in probes if str(run.status or "").upper() not in SAFE_PROBE_STATUSES]
    if unsafe_probes:
        summary = ", ".join(f"#{run.id}:{run.status}" for run in unsafe_probes)
        return VoidEligibility(False, f"存在尚未安全回平的真实探针 {summary}")
    probes_are_flat = bool(probes) and all(str(run.status or "").upper() in SAFE_PROBE_STATUSES for run in probes)

    intents = db.query(ExecutionIntent).filter(ExecutionIntent.hedge_group_id == group.id).all()
    intent_ids = [row.id for row in intents]
    legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id.in_(intent_ids)).all() if intent_ids else []
    leg_ids = [row.id for row in legs]
    venue_orders = db.query(VenueOrder).filter(VenueOrder.execution_leg_id.in_(leg_ids)).all() if leg_ids else []

    profile = str(getattr(group, "execution_profile", "") or "")
    for order in venue_orders:
        external_id = str(order.venue_order_id or "")
        status = str(order.status or "").upper()
        filled = float(order.filled_quantity or 0.0)
        local_paper_fill = profile == "legacy_local" and external_id.startswith("paper-")
        flattened_probe_fill = external_id.startswith("probe:") and probes_are_flat
        if filled > 1e-12 and not (local_paper_fill or flattened_probe_fill):
            return VoidEligibility(False, f"订单 {order.client_order_id} 存在可归属真实成交 {filled:g}，必须先恢复")
        if status not in SAFE_EMPTY_VENUE_ORDER_STATUSES and not (local_paper_fill or flattened_probe_fill):
            return VoidEligibility(False, f"订单 {order.client_order_id} 状态为 {order.status}，结果尚未确认")

    legacy_orders = db.query(Order).filter(Order.hedge_group_id == group.id).all()
    fill_totals = dict(
        db.query(Fill.order_id, func.coalesce(func.sum(Fill.quantity), 0.0))
        .join(Order, Order.id == Fill.order_id)
        .filter(Order.hedge_group_id == group.id)
        .group_by(Fill.order_id)
        .all()
    )
    for order in legacy_orders:
        external_id = str(order.external_order_id or "")
        status = str(order.status or "").lower()
        filled = float(fill_totals.get(order.id, 0.0) or 0.0)
        local_paper_fill = profile == "legacy_local" and external_id.startswith("paper-")
        flattened_probe_fill = external_id.startswith("probe:") and probes_are_flat
        if filled > 1e-12 and not (local_paper_fill or flattened_probe_fill):
            return VoidEligibility(False, f"历史订单 #{order.id} 存在可归属真实成交 {filled:g}，必须先恢复")
        if status not in SAFE_EMPTY_LEGACY_ORDER_STATUSES and not (local_paper_fill or flattened_probe_fill):
            return VoidEligibility(False, f"历史订单 #{order.id} 状态为 {order.status}，结果尚未确认")

    return VoidEligibility(True, "未发现真实敞口或结果未知外部订单，可安全作废归档")


def void_hedge_group(db: Session, group_id: int, *, reason: str, requested_by: str) -> HedgeGroup:
    """在一个数据库事务中软作废对冲组及其未完成投影。调用方负责提交事务。"""
    group = db.query(HedgeGroup).filter(HedgeGroup.id == group_id).with_for_update().one_or_none()
    if group is None:
        raise ValueError("对冲组不存在")
    decision = void_eligibility(db, group)
    if not decision.allowed:
        raise ValueError(decision.reason)

    now = utc_now()
    detail = reason.strip() or "人工作废异常对冲组"
    intents = db.query(ExecutionIntent).filter(ExecutionIntent.hedge_group_id == group.id).all()
    intent_ids = [row.id for row in intents]
    for intent in intents:
        if str(intent.status or "").upper() not in TERMINAL_INTENT_STATUSES:
            intent.status = "VOIDED"
            intent.completed_at = now
            intent.error_message = _append_reason(intent.error_message, f"已作废归档: {detail}")

    legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id.in_(intent_ids)).all() if intent_ids else []
    leg_ids = [row.id for row in legs]
    for leg in legs:
        if str(leg.status or "").upper() not in TERMINAL_LEG_STATUSES:
            leg.status = "CANCELED"

    venue_orders = db.query(VenueOrder).filter(VenueOrder.execution_leg_id.in_(leg_ids)).all() if leg_ids else []
    for order in venue_orders:
        if str(order.status or "").upper() in {"INITIALIZED", "CREATED"}:
            order.status = "CANCELED"

    legacy_orders = db.query(Order).filter(Order.hedge_group_id == group.id).all()
    for order in legacy_orders:
        if str(order.status or "").lower() in {"new", "initialized"}:
            order.status = "canceled"
            order.error_message = _append_reason(order.error_message, f"已作废归档: {detail}")

    if intent_ids:
        outboxes = db.query(ExecutionOutbox).filter(ExecutionOutbox.intent_id.in_(intent_ids)).all()
        for outbox in outboxes:
            if str(outbox.status or "").upper() in {"PENDING", "PROCESSING"}:
                outbox.status = "CANCELED"
                outbox.last_error = _append_reason(outbox.last_error, f"已作废归档: {detail}")

    group.status = "voided"
    group.closed_at = now
    group.close_reason = detail
    group.unrealized_pnl = 0.0
    db.add(HedgeGroupEvent(
        hedge_group_id=group.id,
        event_type="voided",
        detail=f"requested_by={requested_by}; reason={detail}",
    ))
    db.flush()
    return group


def _append_reason(current: str | None, reason: str) -> str:
    current = str(current or "").strip()
    return f"{current}; {reason}" if current else reason
