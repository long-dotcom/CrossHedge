"""旧订单到账户执行事件模型的双写投影。

重构迁移期间，旧执行引擎仍会写 ``orders`` / ``fills``。本模块把这些事实同步为
不可变 ``ExecutionEvent``，并维护 ``ExecutionIntent``、``ExecutionLeg`` 和
``VenueOrder`` 投影。投影使用确定性事件 ID，可被调度器、重启恢复和接口路径安全
重复调用，不会生成重复事件或重复成交。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import (
    ExecutionEvent,
    ExecutionIntent,
    ExecutionLeg,
    Fill,
    HedgeGroup,
    Order,
    VenueOrder,
)


TERMINAL_SUCCESS_STATUSES = {"filled"}
TERMINAL_FAILURE_STATUSES = {"failed", "rejected", "denied", "canceled", "cancelled", "expired", "unfilled", "not_found"}
PARTIAL_STATUSES = {"partially_filled"}
ACTIVE_STATUSES = {"new", "initialized", "released", "emulated", "submitted", "accepted", "pending", "open", "pending_update", "pending_cancel"}


def project_legacy_order(db: Session, order: Order) -> ExecutionEvent | None:
    """把一条旧订单的当前事实幂等投影到新执行模型。"""
    if order.id is None:
        raise ValueError("Order 必须先 flush 后才能投影")
    db.flush()

    existing_venue_order = db.query(VenueOrder).filter(VenueOrder.legacy_order_id == order.id).one_or_none()
    if existing_venue_order is not None and not existing_venue_order.client_order_id.startswith("LEGACY-"):
        # 新 Outbox 路径已经拥有权威 VenueOrder/ExecutionEvent，不再创建影子 Intent。
        return (
            db.query(ExecutionEvent)
            .filter(ExecutionEvent.venue_order_id_ref == existing_venue_order.id)
            .order_by(ExecutionEvent.id.desc())
            .first()
        )

    filled_quantity, average_price, commission = _fill_totals(db, order.id)
    normalized_status = _normalized_status(order.status, filled_quantity, float(order.quantity or 0.0))
    intent = _ensure_shadow_intent(db, order)
    leg = _ensure_shadow_leg(db, order, intent)
    venue_order = _ensure_venue_order(db, order, leg)

    snapshot = {
        "schema_version": 1,
        "source": "legacy_order_projection",
        "legacy_order_id": order.id,
        "hedge_group_id": order.hedge_group_id,
        "platform": str(order.platform or ""),
        "symbol": str(order.symbol or ""),
        "side": str(order.side or "").upper(),
        "status": normalized_status,
        "requested_quantity": float(order.quantity or 0.0),
        "filled_quantity": filled_quantity,
        "remaining_quantity": max(float(order.quantity or 0.0) - filled_quantity, 0.0),
        "average_price": average_price,
        "commission": commission,
        "external_order_id": str(order.external_order_id or ""),
        "position_id": _position_id(order),
        "error_message": str(order.error_message or ""),
    }
    event_id = _event_id(snapshot)
    event = db.query(ExecutionEvent).filter(ExecutionEvent.event_id == event_id).one_or_none()
    if event is None:
        event = ExecutionEvent(
            event_id=event_id,
            intent_id=intent.id,
            execution_leg_id=leg.id,
            venue_order_id_ref=venue_order.id,
            event_type=_event_type(normalized_status, filled_quantity),
            client_order_id=venue_order.client_order_id,
            venue_order_id=str(order.external_order_id or ""),
            ts_event=utc_now(),
            reconciliation=True,
            payload=json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            processed_at=utc_now(),
        )
        db.add(event)

    _apply_snapshot(intent, leg, venue_order, order, snapshot)
    db.flush()
    return event


def project_legacy_orders(db: Session, orders: Iterable[Order]) -> int:
    """批量投影订单，返回处理数量。"""
    count = 0
    for order in orders:
        project_legacy_order(db, order)
        count += 1
    return count


def project_unmirrored_legacy_orders(db: Session, *, limit: int = 200) -> int:
    """分批回填尚无 ``VenueOrder`` 投影的历史订单。"""
    rows = (
        db.query(Order)
        .outerjoin(VenueOrder, VenueOrder.legacy_order_id == Order.id)
        .filter(VenueOrder.id.is_(None))
        .order_by(Order.id)
        .limit(max(int(limit), 1))
        .all()
    )
    return project_legacy_orders(db, rows)


def _ensure_shadow_intent(db: Session, order: Order) -> ExecutionIntent:
    key = f"legacy-order:{order.id}"
    intent = db.query(ExecutionIntent).filter(ExecutionIntent.idempotency_key == key).one_or_none()
    if intent is None:
        group = db.get(HedgeGroup, order.hedge_group_id) if order.hedge_group_id else None
        intent = ExecutionIntent(
            hedge_group_id=order.hedge_group_id,
            intent_type="CLOSE" if order.reduce_only else "OPEN",
            execution_mode=str(getattr(group, "execution_mode", "paper") or "paper"),
            requested_by="legacy-bridge",
            idempotency_key=key,
            status="CREATED",
        )
        db.add(intent)
        db.flush()
    return intent


def _ensure_shadow_leg(db: Session, order: Order, intent: ExecutionIntent) -> ExecutionLeg:
    leg = (
        db.query(ExecutionLeg)
        .filter(ExecutionLeg.intent_id == intent.id, ExecutionLeg.leg_key == "legacy")
        .one_or_none()
    )
    if leg is None:
        leg = ExecutionLeg(
            intent_id=intent.id,
            leg_key="legacy",
            venue=str(order.platform or ""),
            instrument_id=str(order.symbol or ""),
            venue_symbol=str(order.symbol or ""),
            action="CLOSE" if order.reduce_only else "OPEN",
            position_side=_position_side(order),
            order_side=str(order.side or "").upper(),
            strategy_quantity=float(order.quantity or 0.0),
            venue_order_quantity=float(order.quantity or 0.0),
            order_type=str(order.order_type or "market"),
            limit_price=float(order.price) if order.price is not None else None,
            post_only=bool(order.post_only),
            venue_reduce_only=bool(order.reduce_only and str(order.platform).lower() != "binance"),
            status="PLANNED",
        )
        db.add(leg)
        db.flush()
    return leg


def _ensure_venue_order(db: Session, order: Order, leg: ExecutionLeg) -> VenueOrder:
    venue_order = db.query(VenueOrder).filter(VenueOrder.legacy_order_id == order.id).one_or_none()
    if venue_order is None:
        venue_order = VenueOrder(
            execution_leg_id=leg.id,
            legacy_order_id=order.id,
            client_order_id=f"LEGACY-{order.id}",
            venue_order_id=str(order.external_order_id or ""),
            position_id=_position_id(order),
            status="INITIALIZED",
            requested_quantity=float(order.quantity or 0.0),
            filled_quantity=0.0,
            remaining_quantity=float(order.quantity or 0.0),
            reconciliation_state="LEGACY_BRIDGE",
        )
        db.add(venue_order)
        db.flush()
    return venue_order


def _apply_snapshot(
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    venue_order: VenueOrder,
    order: Order,
    snapshot: dict[str, Any],
) -> None:
    status = str(snapshot["status"])
    venue_order.venue_order_id = str(snapshot["external_order_id"])
    venue_order.position_id = str(snapshot["position_id"])
    venue_order.status = status.upper()
    venue_order.requested_quantity = float(snapshot["requested_quantity"])
    venue_order.filled_quantity = float(snapshot["filled_quantity"])
    venue_order.remaining_quantity = float(snapshot["remaining_quantity"])
    venue_order.average_price = float(snapshot["average_price"]) or None
    venue_order.commission = float(snapshot["commission"])
    venue_order.raw_last_report = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    venue_order.last_event_at = utc_now()

    if status in TERMINAL_SUCCESS_STATUSES:
        leg.status = "FILLED"
        intent.status = "COMPLETED"
        intent.completed_at = intent.completed_at or utc_now()
    elif status in TERMINAL_FAILURE_STATUSES:
        leg.status = "FAILED"
        intent.status = "FAILED"
        intent.error_message = str(order.error_message or status)
        intent.completed_at = intent.completed_at or utc_now()
    elif status in PARTIAL_STATUSES or float(snapshot["filled_quantity"]) > 0:
        leg.status = "PARTIALLY_FILLED"
        intent.status = "RUNNING"
        intent.error_message = str(order.error_message or "")
    else:
        leg.status = "SUBMITTED" if status in ACTIVE_STATUSES else "UNKNOWN"
        intent.status = "RUNNING"
        intent.error_message = str(order.error_message or "")


def _fill_totals(db: Session, order_id: int) -> tuple[float, float, float]:
    fills = db.query(Fill).filter(Fill.order_id == order_id).order_by(Fill.id).all()
    quantity = sum(abs(float(fill.quantity or 0.0)) for fill in fills)
    notional = sum(abs(float(fill.quantity or 0.0)) * float(fill.price or 0.0) for fill in fills)
    commission = sum(float(fill.fee or 0.0) for fill in fills)
    average_price = notional / quantity if quantity > 0 else float(0.0)
    return quantity, average_price, commission


def _normalized_status(raw_status: str, filled_quantity: float, requested_quantity: float) -> str:
    status = str(raw_status or "new").strip().lower()
    if filled_quantity > 0 and requested_quantity > 0:
        if filled_quantity + 1e-12 >= requested_quantity:
            return "filled"
        if status not in TERMINAL_FAILURE_STATUSES:
            return "partially_filled"
    return status


def _event_type(status: str, filled_quantity: float) -> str:
    if status == "filled":
        return "ORDER_FILLED"
    if status == "partially_filled" or filled_quantity > 0:
        return "ORDER_PARTIALLY_FILLED"
    if status in {"canceled", "cancelled"}:
        return "ORDER_CANCELED"
    if status in TERMINAL_FAILURE_STATUSES:
        return "ORDER_REJECTED"
    if status in {"accepted", "open"}:
        return "ORDER_ACCEPTED"
    return "ORDER_SUBMITTED"


def _position_side(order: Order) -> str:
    if str(order.platform or "").strip().lower() != "binance":
        return "NET"
    is_buy = str(order.side or "").strip().lower() in {"buy", "long"}
    if order.reduce_only:
        return "SHORT" if is_buy else "LONG"
    return "LONG" if is_buy else "SHORT"


def _position_id(order: Order) -> str:
    side = _position_side(order)
    if side == "NET":
        return ""
    return f"{order.symbol}-{side}"


def _event_id(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"legacy:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:56]}"
