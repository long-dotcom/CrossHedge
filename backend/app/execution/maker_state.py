"""持久化 Maker-then-Market 状态机。

每次状态推进只读取 Intent/Leg/VenueOrder/Outbox 的持久化事实。Maker 新增成交会
按目标比例生成独立 HEDGE 腿；TTL 到期必须先确认撤单终态，之后才允许市价兜底。
任何动态腿都有独立 Outbox 与稳定 ClientOrderId，因此重启不会重复发送。
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import (
    ArbitrageOpportunity, ExecutionIntent, ExecutionLeg, ExecutionOutbox,
    HedgeGroup, HedgeGroupEvent, VenueOrder,
)
from app.execution.intents import ExecutionLegPlan, enqueue_execution_leg


NON_TERMINAL_LEG_STATUSES = {"PLANNED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}
FAILURE_LEG_STATUSES = {"FAILED"}
AUTO_FLATTEN_ACTIONS = {"auto_close", "reverse_filled_leg", "auto_flatten_filled_leg"}


def advance_maker_intent(
    db: Session,
    *,
    intent: ExecutionIntent,
    current_outbox: ExecutionOutbox,
    legs: list[ExecutionLeg],
    adapter_factory: Callable[[str, str], Any] | None = None,
) -> bool:
    """推进一个 Maker Intent；返回 ``True`` 表示已接管通用完成投影。"""
    if str(intent.execution_style or "") != "maker_then_market":
        return False
    root_outbox, spec = _maker_spec(db, intent.id)
    if root_outbox is None or not spec:
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = "Maker Intent 缺少持久化状态机参数"
        _project_recovery(db, intent, intent.error_message)
        return True

    # 原生事件可能在另一个尚未领取的动态 Outbox 存在时到达；绝不能把 PENDING
    # 命令误标为已发送。只有本轮真正领取过的命令才完成状态转换。
    if current_outbox.status == "PROCESSING":
        current_outbox.status = "SENT"
        current_outbox.locked_at = None
    orders = _orders_by_leg(db, legs)
    maker_legs = [leg for leg in legs if leg.role in {"MAKER", "MAKER_FALLBACK"}]
    hedge_legs = [leg for leg in legs if leg.role == "HEDGE"]
    compensation_legs = [leg for leg in legs if leg.role == "COMPENSATION"]
    if not maker_legs:
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = "Maker Intent 不存在 MAKER 腿"
        _project_recovery(db, intent, intent.error_message)
        return True

    maker_target = float(spec.get("maker_target_quantity") or 0.0)
    hedge_target = float(spec.get("hedge_target_quantity") or 0.0)
    if maker_target <= 0 or hedge_target <= 0:
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = "Maker/Hedge 目标数量无效"
        _project_recovery(db, intent, intent.error_message)
        return True

    primary = next((leg for leg in maker_legs if leg.role == "MAKER"), maker_legs[0])
    primary_order = orders.get(primary.id)
    if primary_order is not None and _maker_ttl_expired(primary, primary_order, spec):
        if not _request_cancel(db, intent, primary, primary_order, adapter_factory):
            return True

    maker_filled = _filled(orders, maker_legs)
    maker_submitted = sum(float(leg.venue_order_quantity or 0.0) for leg in maker_legs)
    maker_active = any(leg.status in NON_TERMINAL_LEG_STATUSES for leg in maker_legs)
    fallback_action = str(spec.get("maker_unfilled_action") or "cancel")

    fallback_submitted = sum(
        float(leg.venue_order_quantity or 0.0)
        for leg in maker_legs if leg.role == "MAKER_FALLBACK"
    )
    fallback_remaining = max(maker_target - maker_filled - fallback_submitted, 0.0)
    if not maker_active and fallback_remaining > _tol(maker_target):
        if fallback_action == "market_fallback":
            _enqueue_fallback(db, intent, primary, fallback_remaining, len(maker_legs), spec)
            intent.status = "RUNNING"
            return True

    maker_filled = _filled(orders, maker_legs)
    desired_hedge = hedge_target * min(max(maker_filled / maker_target, 0.0), 1.0)
    hedge_submitted = sum(float(leg.venue_order_quantity or 0.0) for leg in hedge_legs)
    hedge_delta = max(desired_hedge - hedge_submitted, 0.0)
    if hedge_delta > _tol(hedge_target):
        _enqueue_hedge(db, intent, primary, hedge_delta, len(hedge_legs), spec)
        intent.status = "RUNNING"
        return True

    hedge_filled = _filled(orders, hedge_legs)
    failed_hedge = any(leg.status in FAILURE_LEG_STATUSES for leg in hedge_legs)
    if failed_hedge and hedge_filled < desired_hedge - _tol(hedge_target):
        return _handle_failed_hedge(
            db, intent, primary, maker_filled, hedge_filled,
            maker_target, hedge_target, compensation_legs, orders, spec,
        )

    maker_active = any(leg.status in NON_TERMINAL_LEG_STATUSES for leg in maker_legs)
    hedge_active = any(leg.status in NON_TERMINAL_LEG_STATUSES for leg in hedge_legs)
    maker_done = not maker_active and (
        maker_filled >= maker_target - _tol(maker_target)
        or fallback_action == "cancel"
        or maker_submitted >= maker_target - _tol(maker_target)
    )
    hedge_done = not hedge_active and hedge_filled >= desired_hedge - _tol(hedge_target)
    if maker_done and hedge_done:
        matched_ratio = min(
            maker_filled / maker_target if maker_target > 0 else 0.0,
            hedge_filled / hedge_target if hedge_target > 0 else 0.0,
            1.0,
        )
        _complete_maker_intent(db, intent, primary, spec, matched_ratio)
    else:
        intent.status = "RUNNING"
    return True


def _maker_spec(db: Session, intent_id: int) -> tuple[ExecutionOutbox | None, dict[str, Any]]:
    rows = db.query(ExecutionOutbox).filter(ExecutionOutbox.intent_id == intent_id).order_by(ExecutionOutbox.id).all()
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError):
            continue
        if int(payload.get("maker_state_version") or 0) == 1:
            return row, payload
    return None, {}


def _orders_by_leg(db: Session, legs: list[ExecutionLeg]) -> dict[int, VenueOrder]:
    ids = [leg.id for leg in legs]
    if not ids:
        return {}
    return {row.execution_leg_id: row for row in db.query(VenueOrder).filter(VenueOrder.execution_leg_id.in_(ids)).all()}


def _filled(orders: dict[int, VenueOrder], legs: list[ExecutionLeg]) -> float:
    return sum(float(orders[leg.id].filled_quantity or 0.0) for leg in legs if leg.id in orders)


def _maker_ttl_expired(leg: ExecutionLeg, order: VenueOrder, spec: dict[str, Any]) -> bool:
    if leg.role != "MAKER" or not leg.post_only or leg.status not in NON_TERMINAL_LEG_STATUSES:
        return False
    if str(order.status or "").upper() == "PENDING_CANCEL":
        return True
    ttl = max(int(spec.get("maker_ttl_seconds") or 0), 0)
    return ttl > 0 and (utc_now() - leg.created_at).total_seconds() >= ttl


def _request_cancel(
    db: Session,
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    order: VenueOrder,
    adapter_factory: Callable[[str, str], Any] | None,
) -> bool:
    if str(order.status or "").upper() == "PENDING_CANCEL":
        intent.status = "RUNNING"
        return False
    # 撤单也是外部副作用，必须先持久化 Outbox；状态机线程不得直接调用 venue。
    payload = {
        "schema_version": 1,
        "command_kind": "CANCEL_ORDER",
        "intent_id": intent.id,
        "execution_mode": intent.execution_mode,
        "dispatch_leg_ids": [leg.id],
        "client_order_id": order.client_order_id,
    }
    db.add(ExecutionOutbox(
        intent_id=intent.id,
        event_type="CANCEL_ORDER_REQUESTED",
        payload=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        status="PENDING",
    ))
    order.status = "PENDING_CANCEL"
    order.reconciliation_state = "CANCEL_QUEUED"
    leg.status = "SUBMITTED"
    intent.status = "RUNNING"
    return False


def _enqueue_fallback(
    db: Session,
    intent: ExecutionIntent,
    primary: ExecutionLeg,
    quantity: float,
    sequence: int,
    spec: dict[str, Any],
) -> None:
    plan = _copy_plan(
        primary,
        leg_key=f"maker_fallback_{sequence}",
        role="MAKER_FALLBACK",
        parent_leg_id=primary.id,
        sequence=sequence,
        strategy_quantity=float(primary.strategy_quantity) * quantity / float(primary.venue_order_quantity),
        venue_order_quantity=quantity,
        order_type="market",
        limit_price=None,
        post_only=False,
    )
    enqueue_execution_leg(
        db, intent=intent, plan=plan, event_type="MAKER_FALLBACK_CREATED",
        command_payload={"maker_state_version": 1, "root_intent_id": intent.id},
    )


def _enqueue_hedge(
    db: Session,
    intent: ExecutionIntent,
    primary: ExecutionLeg,
    quantity: float,
    sequence: int,
    spec: dict[str, Any],
) -> None:
    template = dict(spec.get("hedge_template") or {})
    target = float(spec.get("hedge_target_quantity") or 0.0)
    template.update({
        "leg_key": f"hedge_{sequence}",
        "role": "HEDGE",
        "parent_leg_id": primary.id,
        "sequence": sequence,
        "strategy_quantity": float(template.get("strategy_quantity") or target) * quantity / target,
        "venue_order_quantity": quantity,
        "order_type": "market",
        "limit_price": None,
        "post_only": False,
    })
    plan = _plan_from_dict(template)
    enqueue_execution_leg(
        db, intent=intent, plan=plan, event_type="HEDGE_INCREMENT_CREATED",
        command_payload={"maker_state_version": 1, "root_intent_id": intent.id},
    )


def _handle_failed_hedge(
    db: Session,
    intent: ExecutionIntent,
    primary: ExecutionLeg,
    maker_filled: float,
    hedge_filled: float,
    maker_target: float,
    hedge_target: float,
    compensation_legs: list[ExecutionLeg],
    orders: dict[int, VenueOrder],
    spec: dict[str, Any],
) -> bool:
    matched_maker = maker_target * min(max(hedge_filled / hedge_target, 0.0), 1.0)
    unmatched = max(maker_filled - matched_maker, 0.0)
    action = str(spec.get("single_leg_action") or "manual_intervention").lower()
    if action not in AUTO_FLATTEN_ACTIONS or unmatched <= _tol(maker_target):
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = f"Maker 已成交 {maker_filled:g}，Hedge 仅成交 {hedge_filled:g}，需要恢复"
        _project_recovery(db, intent, intent.error_message)
        return True
    if not compensation_legs:
        plan = _copy_plan(
            primary,
            leg_key="compensation_0",
            role="COMPENSATION",
            parent_leg_id=primary.id,
            sequence=0,
            order_side="SELL" if primary.order_side.upper() == "BUY" else "BUY",
            action="COMPENSATE",
            strategy_quantity=float(primary.strategy_quantity) * unmatched / maker_target,
            venue_order_quantity=unmatched,
            order_type="market",
            limit_price=None,
            post_only=False,
            venue_reduce_only=primary.venue != "binance",
        )
        enqueue_execution_leg(
            db, intent=intent, plan=plan, event_type="COMPENSATION_CREATED",
            command_payload={"maker_state_version": 1, "root_intent_id": intent.id},
        )
        intent.status = "RUNNING"
        return True
    compensation_filled = _filled(orders, compensation_legs)
    if any(leg.status == "FAILED" for leg in compensation_legs) or compensation_filled < unmatched - _tol(maker_target):
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = f"自动补偿未完成: target={unmatched:g}, filled={compensation_filled:g}"
        _project_recovery(db, intent, intent.error_message)
        return True
    matched_ratio = min(hedge_filled / hedge_target, maker_filled / maker_target, 1.0)
    _complete_maker_intent(db, intent, primary, spec, matched_ratio)
    return True


def _complete_maker_intent(
    db: Session,
    intent: ExecutionIntent,
    primary: ExecutionLeg,
    spec: dict[str, Any],
    matched_ratio: float,
) -> None:
    group = db.get(HedgeGroup, intent.hedge_group_id) if intent.hedge_group_id else None
    ratio = min(max(float(matched_ratio), 0.0), 1.0)
    intent.completed_at = intent.completed_at or utc_now()
    if group is None:
        intent.status = "COMPLETED" if ratio > 0 else "FAILED"
        if ratio <= 1e-12 and not intent.error_message:
            intent.error_message = "Maker 到期未成交，未建立敞口"
        return
    event_type = f"maker_{intent.intent_type.lower()}_completed"
    if _group_event_exists(db, group.id, event_type):
        return
    hedge_template = dict(spec.get("hedge_template") or {})
    if intent.intent_type == "OPEN":
        if ratio <= 1e-12:
            group.status = "failed"
            # 若交易场所已给出拒绝原因，保留真实原因，不再被 TTL 泛化文案覆盖。
            intent.error_message = intent.error_message or "Maker 到期未成交，未建立敞口"
            group.close_reason = intent.error_message
            intent.status = "FAILED"
        else:
            group.status = "open" if ratio >= 1.0 - 1e-9 else "open_partial"
            _set_group_leg_quantity(group, primary.leg_key, float(primary.strategy_quantity) * ratio)
            _set_group_leg_quantity(group, str(hedge_template.get("leg_key") or ""), float(hedge_template.get("strategy_quantity") or 0.0) * ratio)
            group.opened_at = group.opened_at or utc_now()
            group.close_reason = "" if group.status == "open" else f"Maker 到期后建立 {ratio:.2%} 部分对冲"
            intent.status = "COMPLETED"
        opportunity_id = int(spec.get("opportunity_id") or 0)
        opportunity = db.get(ArbitrageOpportunity, opportunity_id) if opportunity_id else None
        if opportunity is not None:
            opportunity.status = "executed" if ratio > 0 else "rejected"
            opportunity.reject_reason = group.close_reason
        if ratio > 0:
            # Maker 与后续 Hedge 都已形成成交事实后，才写入真实开仓价差。
            from app.execution.pnl import actual_entry_spread_from_fills

            actual_entry_spread = actual_entry_spread_from_fills(db, group)
            if actual_entry_spread is not None:
                group.entry_spread = actual_entry_spread
    else:
        if ratio >= 1.0 - 1e-9:
            group.status = "closed"
            group.closed_at = group.closed_at or utc_now()
            group.close_reason = str(spec.get("reason") or "Maker 平仓完成")
        elif ratio > 1e-12:
            group.status = "open_partial"
            _set_group_leg_quantity(group, primary.leg_key, max(_group_leg_quantity(group, primary.leg_key) - float(primary.strategy_quantity) * ratio, 0.0))
            key = str(hedge_template.get("leg_key") or "")
            _set_group_leg_quantity(group, key, max(_group_leg_quantity(group, key) - float(hedge_template.get("strategy_quantity") or 0.0) * ratio, 0.0))
            group.close_reason = f"Maker 到期后完成 {ratio:.2%} 部分平仓"
        else:
            group.status = str(spec.get("previous_group_status") or "open")
            intent.error_message = intent.error_message or "Maker 到期未成交，平仓取消"
            group.close_reason = intent.error_message
        intent.status = "COMPLETED" if ratio > 0 else "FAILED"
    db.add(HedgeGroupEvent(
        hedge_group_id=group.id,
        event_type=event_type,
        detail=f"Intent #{intent.id}; matched_ratio={ratio:.12f}",
    ))


def _project_recovery(db: Session, intent: ExecutionIntent, detail: str) -> None:
    group = db.get(HedgeGroup, intent.hedge_group_id) if intent.hedge_group_id else None
    if group is None:
        return
    group.status = "manual_intervention"
    group.close_reason = detail
    if not _group_event_exists(db, group.id, "maker_recovery_required"):
        db.add(HedgeGroupEvent(
            hedge_group_id=group.id,
            event_type="maker_recovery_required",
            detail=f"Intent #{intent.id}; {detail}",
        ))


def _copy_plan(leg: ExecutionLeg, **changes: Any) -> ExecutionLegPlan:
    values = {field.name: getattr(leg, field.name) for field in fields(ExecutionLegPlan)}
    values.update(changes)
    return ExecutionLegPlan(**values)


def _plan_from_dict(payload: dict[str, Any]) -> ExecutionLegPlan:
    names = {field.name for field in fields(ExecutionLegPlan)}
    return ExecutionLegPlan(**{name: payload[name] for name in names if name in payload})


def _group_event_exists(db: Session, group_id: int, event_type: str) -> bool:
    return db.query(HedgeGroupEvent.id).filter(
        HedgeGroupEvent.hedge_group_id == group_id,
        HedgeGroupEvent.event_type == event_type,
    ).first() is not None


def _set_group_leg_quantity(group: HedgeGroup, leg_key: str, quantity: float) -> None:
    if leg_key == "leg_a":
        group.leg_a_quantity = quantity
    elif leg_key == "leg_b":
        group.leg_b_quantity = quantity


def _group_leg_quantity(group: HedgeGroup, leg_key: str) -> float:
    if leg_key == "leg_a":
        return float(group.leg_a_quantity or 0.0)
    if leg_key == "leg_b":
        return float(group.leg_b_quantity or 0.0)
    return 0.0


def _tol(quantity: float) -> float:
    return max(abs(float(quantity)) * 1e-9, 1e-12)
