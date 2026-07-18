"""从不可变 ExecutionEvent 重建执行投影。

该工具用于灾难恢复和一致性审计：先清空指定 Intent 的可变订单/腿投影，再按事件
顺序重放，最后重新计算 Intent 与普通 OPEN/CLOSE/RECOVER 对冲组状态。事件表本身
不会被修改。Maker/Probe 的业务编排包含动态派生命令，需使用各自状态机恢复，
本工具会明确拒绝，避免离线重放时生成新的交易副作用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import ExecutionEvent, ExecutionIntent, ExecutionLeg, ExecutionOutbox, VenueOrder


FAILURE_STATUSES = {"FAILED", "REJECTED", "DENIED", "CANCELED", "CANCELLED", "EXPIRED", "UNFILLED"}
NON_TERMINAL_STATUSES = {"INITIALIZED", "NEW", "SUBMITTED", "ACCEPTED", "PENDING", "OPEN", "PARTIALLY_FILLED", "UNKNOWN"}


@dataclass(frozen=True, slots=True)
class ReplayResult:
    intent_id: int
    event_count: int
    order_count: int
    intent_status: str


def rebuild_intent_projection(db: Session, intent_id: int) -> ReplayResult:
    """在当前事务中从不可变事件重建一个普通执行 Intent 的可变投影。"""
    intent = db.get(ExecutionIntent, int(intent_id))
    if intent is None:
        raise ValueError(f"ExecutionIntent #{intent_id} 不存在")
    if intent.intent_type in {"PROBE_ENTRY", "PROBE_EXIT"} or intent.execution_style == "maker_then_market":
        raise ValueError("Maker/Probe 必须使用专用状态机恢复，禁止通用重放生成派生命令")

    legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
    if not legs:
        raise ValueError(f"ExecutionIntent #{intent.id} 没有执行腿")
    orders = (
        db.query(VenueOrder)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .filter(ExecutionLeg.intent_id == intent.id)
        .order_by(VenueOrder.id)
        .all()
    )
    by_id = {row.id: row for row in orders}
    by_client_id = {row.client_order_id: row for row in orders}
    leg_by_id = {row.id: row for row in legs}

    for leg in legs:
        leg.status = "PLANNED"
    for order in orders:
        order.status = "INITIALIZED"
        order.venue_order_id = ""
        order.filled_quantity = 0.0
        order.remaining_quantity = float(order.requested_quantity or 0.0)
        order.average_price = None
        order.commission = 0.0
        order.reconciliation_state = "REPLAY"
        order.raw_last_report = ""
        order.last_event_at = None

    events = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.intent_id == intent.id)
        .order_by(ExecutionEvent.ts_event, ExecutionEvent.id)
        .all()
    )
    for event in events:
        order = by_id.get(event.venue_order_id_ref) or by_client_id.get(event.client_order_id)
        if order is None:
            continue
        leg = leg_by_id.get(order.execution_leg_id)
        if leg is None:
            continue
        payload = _payload(event.payload)
        _apply_event(order, leg, event, payload)

    statuses = {str(leg.status or "PLANNED").upper() for leg in legs}
    if statuses and statuses <= {"FILLED"}:
        intent.status = "COMPLETED"
        intent.completed_at = intent.completed_at or utc_now()
        intent.error_message = ""
    elif statuses & {"FAILED"}:
        if statuses & {"PLANNED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}:
            intent.status = "RECOVERY_REQUIRED"
        else:
            intent.status = "FAILED"
    else:
        intent.status = "RUNNING"
        intent.completed_at = None

    outbox = db.query(ExecutionOutbox).filter(
        ExecutionOutbox.intent_id == intent.id,
    ).order_by(ExecutionOutbox.id.desc()).first()
    if outbox is not None and intent.intent_type in {"OPEN", "CLOSE", "RECOVER"}:
        from app.execution.outbox_worker import _project_hedge_group_state

        _project_hedge_group_state(db, outbox, intent, legs)
    db.flush()
    return ReplayResult(intent.id, len(events), len(orders), intent.status)


def _payload(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _apply_event(
    order: VenueOrder,
    leg: ExecutionLeg,
    event: ExecutionEvent,
    payload: dict,
) -> None:
    status = str(payload.get("status") or _status_from_event(event.event_type)).upper()
    venue_order_id = str(payload.get("external_order_id") or payload.get("venue_order_id") or event.venue_order_id or "")
    if venue_order_id:
        order.venue_order_id = venue_order_id
    filled = abs(float(payload.get("filled_quantity") or payload.get("filled") or 0.0))
    if event.event_type == "OrderFilled":
        filled = float(order.filled_quantity or 0.0) + abs(float(payload.get("last_qty") or 0.0))
    order.filled_quantity = max(float(order.filled_quantity or 0.0), filled)
    order.remaining_quantity = max(float(order.requested_quantity or 0.0) - order.filled_quantity, 0.0)
    price = float(payload.get("average_price") or payload.get("last_px") or 0.0)
    if price > 0:
        if event.event_type == "OrderFilled" and order.average_price and order.filled_quantity > 0:
            previous = max(order.filled_quantity - abs(float(payload.get("last_qty") or 0.0)), 0.0)
            order.average_price = (float(order.average_price) * previous + price * (order.filled_quantity - previous)) / order.filled_quantity
        else:
            order.average_price = price
    commission = float(payload.get("fee") or payload.get("commission") or 0.0)
    order.commission = (
        float(order.commission or 0.0) + commission
        if event.event_type == "OrderFilled" else max(float(order.commission or 0.0), commission)
    )
    order.raw_last_report = event.payload
    order.last_event_at = event.ts_event or event.ts_init or event.created_at
    order.reconciliation_state = "REPLAY"
    if order.remaining_quantity <= 1e-12 and order.filled_quantity > 0:
        order.status = "FILLED"
        leg.status = "FILLED"
    elif status in FAILURE_STATUSES:
        order.status = status
        leg.status = "PARTIALLY_FILLED" if order.filled_quantity > 0 else "FAILED"
    elif order.filled_quantity > 0:
        order.status = "PARTIALLY_FILLED"
        leg.status = "PARTIALLY_FILLED"
    else:
        order.status = status
        leg.status = "SUBMITTED" if status not in {"INITIALIZED", "UNKNOWN"} else status


def _status_from_event(event_type: str) -> str:
    name = str(event_type or "").upper()
    if "FILLED" in name:
        return "FILLED"
    if "REJECT" in name:
        return "REJECTED"
    if "CANCEL" in name:
        return "CANCELED"
    if "EXPIRE" in name:
        return "EXPIRED"
    if "ACCEPT" in name:
        return "ACCEPTED"
    if "SUBMIT" in name:
        return "SUBMITTED"
    return "UNKNOWN"
