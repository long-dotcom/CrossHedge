"""统一原生 VenueEvent 的持久化和订单投影。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from enum import Enum
from queue import Empty, SimpleQueue
from typing import Any

from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import (
    ExecutionEvent,
    ExecutionIntent,
    ExecutionLeg,
    ExecutionOutbox,
    Fill as DatabaseFill,
    Order,
    VenueOrder,
)
from app.db.session import SessionLocal
from app.venues.domain.events import VenueEvent

venue_event_queue: SimpleQueue[VenueEvent] = SimpleQueue()


def enqueue_venue_event(event: VenueEvent) -> None:
    """WS/MT5 轮询回调只入队，不在网络线程执行数据库操作。"""
    venue_event_queue.put(event)


def project_venue_events_once(
    *,
    session_factory: sessionmaker = SessionLocal,
    limit: int = 1000,
) -> int:
    events: list[VenueEvent] = []
    for _ in range(max(int(limit), 1)):
        try:
            events.append(venue_event_queue.get_nowait())
        except Empty:
            break
    if not events:
        return 0
    inserted = 0
    with session_factory() as db:
        touched: set[int] = set()
        for event in events:
            changed, intent_id = _project_event(db, event)
            inserted += int(changed)
            if intent_id is not None:
                touched.add(intent_id)
        db.flush()
        _advance_intents(db, touched)
        db.commit()
    return inserted


def _project_event(db, event: VenueEvent) -> tuple[bool, int | None]:
    event_id = _database_event_id(event)
    if db.query(ExecutionEvent.id).filter(ExecutionEvent.event_id == event_id).first() is not None:
        return False, None
    client_order_id = ""
    venue_order_id = ""
    if event.order is not None:
        client_order_id = event.order.client_order_id
        venue_order_id = event.order.venue_order_id
    elif event.fill is not None:
        client_order_id = event.fill.client_order_id
        venue_order_id = event.fill.venue_order_id
    venue_order = _find_venue_order(db, client_order_id, venue_order_id)
    leg = db.get(ExecutionLeg, venue_order.execution_leg_id) if venue_order is not None else None
    intent_id = int(leg.intent_id) if leg is not None else None
    payload = json.dumps(asdict(event), ensure_ascii=False, sort_keys=True, default=_json_default, separators=(",", ":"))
    db.add(
        ExecutionEvent(
            event_id=event_id,
            intent_id=intent_id,
            execution_leg_id=leg.id if leg else None,
            venue_order_id_ref=venue_order.id if venue_order else None,
            event_type=event.event_type.value,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            ts_event=event.occurred_at,
            ts_init=event.received_at,
            reconciliation=event.reconciliation,
            payload=payload,
            processed_at=utc_now(),
        )
    )
    if venue_order is None or leg is None:
        return True, None
    if event.order is not None:
        _apply_order(venue_order, leg, event.order)
    if event.fill is not None:
        _apply_fill(db, venue_order, leg, event.fill)
    return True, intent_id


def _find_venue_order(db, client_order_id: str, venue_order_id: str) -> VenueOrder | None:
    if client_order_id:
        row = db.query(VenueOrder).filter(VenueOrder.client_order_id == client_order_id).one_or_none()
        if row is not None:
            return row
    if venue_order_id:
        return (
            db.query(VenueOrder)
            .filter(VenueOrder.venue_order_id == venue_order_id)
            .order_by(VenueOrder.id.desc())
            .first()
        )
    return None


def _apply_order(venue_order: VenueOrder, leg: ExecutionLeg, snapshot) -> None:
    venue_order.venue_order_id = snapshot.venue_order_id or venue_order.venue_order_id
    venue_order.status = snapshot.status.value
    venue_order.filled_quantity = max(float(venue_order.filled_quantity or 0), float(snapshot.filled_quantity))
    venue_order.remaining_quantity = max(float(snapshot.remaining_quantity), 0.0)
    if snapshot.average_price is not None and snapshot.average_price > 0:
        venue_order.average_price = float(snapshot.average_price)
    venue_order.commission = max(float(venue_order.commission or 0), float(snapshot.commission))
    venue_order.reconciliation_state = "VENUE_EVENT"
    venue_order.last_event_at = snapshot.updated_at
    venue_order.raw_last_report = json.dumps(snapshot.raw, ensure_ascii=False, default=_json_default)
    leg.status = _leg_status(snapshot.status.value, venue_order.filled_quantity)


def _apply_fill(db, venue_order: VenueOrder, leg: ExecutionLeg, fill) -> None:
    existing_quantity, existing_notional, existing_commission = _projected_fill_totals(db, venue_order.id)
    event_quantity = existing_quantity + float(fill.quantity)
    event_notional = existing_notional + float(fill.quantity * fill.price)
    cumulative = max(float(venue_order.filled_quantity or 0), event_quantity)
    venue_order.filled_quantity = cumulative
    venue_order.remaining_quantity = max(float(venue_order.requested_quantity or 0) - cumulative, 0.0)
    if event_quantity > 0:
        venue_order.average_price = event_notional / event_quantity
    venue_order.commission = existing_commission + float(fill.commission)
    venue_order.status = "FILLED" if venue_order.remaining_quantity <= 1e-12 else "PARTIALLY_FILLED"
    venue_order.reconciliation_state = "VENUE_FILL"
    venue_order.last_event_at = fill.occurred_at
    leg.status = "FILLED" if venue_order.status == "FILLED" else "PARTIALLY_FILLED"
    if venue_order.legacy_order_id is not None:
        order = db.get(Order, venue_order.legacy_order_id)
        if order is not None:
            order.status = venue_order.status.lower()
            order.external_order_id = venue_order.venue_order_id
            order.price = venue_order.average_price
            db.add(
                DatabaseFill(
                    order_id=order.id,
                    platform=fill.venue,
                    symbol=order.symbol,
                    side=fill.side.value,
                    quantity=float(fill.quantity),
                    price=float(fill.price),
                    fee=float(fill.commission),
                )
            )


def _projected_fill_totals(db, venue_order_id: int) -> tuple[float, float, float]:
    rows = db.query(ExecutionEvent.payload).filter(
        ExecutionEvent.venue_order_id_ref == venue_order_id,
        ExecutionEvent.event_type == "FILL",
    ).all()
    quantity = 0.0
    notional = 0.0
    commission = 0.0
    for (payload,) in rows:
        try:
            data = json.loads(payload or "{}")
            fill = data.get("fill") or {}
            fill_quantity = abs(float(fill.get("quantity") or 0))
            quantity += fill_quantity
            notional += fill_quantity * float(fill.get("price") or 0)
            commission += float(fill.get("commission") or 0)
        except (TypeError, ValueError):
            continue
    return quantity, notional, commission


def _advance_intents(db, intent_ids: set[int]) -> None:
    from app.execution.outbox_worker import _finish_command

    for intent_id in intent_ids:
        intent = db.get(ExecutionIntent, intent_id)
        if intent is None:
            continue
        outbox = (
            db.query(ExecutionOutbox)
            .filter(ExecutionOutbox.intent_id == intent_id)
            .order_by(ExecutionOutbox.id.desc())
            .first()
        )
        legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent_id).order_by(ExecutionLeg.id).all()
        if outbox is not None and legs:
            _finish_command(db, outbox, intent, legs)


def _leg_status(order_status: str, filled_quantity: float) -> str:
    if order_status == "FILLED":
        return "FILLED"
    if order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
        return "PARTIALLY_FILLED" if filled_quantity > 0 else "FAILED"
    if filled_quantity > 0:
        return "PARTIALLY_FILLED"
    return "SUBMITTED"


def _database_event_id(event: VenueEvent) -> str:
    raw = f"venue:{event.event_id}"
    if len(raw) <= 64:
        return raw
    return f"venue:{hashlib.sha256(raw.encode()).hexdigest()[:58]}"


def _json_default(value: Any):
    if isinstance(value, (Decimal, datetime)):
        return str(value) if isinstance(value, Decimal) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"不能序列化: {type(value)!r}")
