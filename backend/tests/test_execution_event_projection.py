"""旧订单双写到下一代执行事件模型的测试。"""

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
    ExecutionEvent,
    ExecutionIntent,
    ExecutionLeg,
    Fill,
    HedgeGroup,
    Order,
    VenueOrder,
)
from app.execution.event_projection import project_legacy_order


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _order(db, *, side="sell", reduce_only=True, status="submitted") -> Order:
    group = HedgeGroup(
        symbol="GOLD", direction="long_leg_a_short_leg_b", status="closing",
        execution_mode="live", notional=4000, quantity=1.0,
    )
    db.add(group)
    db.flush()
    order = Order(
        hedge_group_id=group.id,
        platform="binance",
        symbol="XAUUSDT-PERP.BINANCE",
        side=side,
        quantity=0.01,
        order_type="market",
        reduce_only=reduce_only,
        status=status,
        external_order_id="venue-1",
    )
    db.add(order)
    db.flush()
    return order


def test_projection_creates_idempotent_event_and_hedge_position_id() -> None:
    db = _session()
    order = _order(db)

    first = project_legacy_order(db, order)
    second = project_legacy_order(db, order)

    assert first.id == second.id
    assert db.query(ExecutionEvent).count() == 1
    assert db.query(ExecutionIntent).one().intent_type == "CLOSE"
    leg = db.query(ExecutionLeg).one()
    assert leg.position_side == "LONG"
    assert leg.venue_reduce_only is False
    projected = db.query(VenueOrder).one()
    assert projected.position_id == "XAUUSDT-PERP.BINANCE-LONG"
    assert projected.status == "SUBMITTED"


def test_later_fill_appends_event_and_advances_projection() -> None:
    db = _session()
    order = _order(db)
    project_legacy_order(db, order)
    db.add(Fill(
        order_id=order.id, platform="binance", symbol=order.symbol,
        side=order.side, quantity=0.01, price=3998.5, fee=0.015,
    ))
    order.status = "filled"

    event = project_legacy_order(db, order)

    assert db.query(ExecutionEvent).count() == 2
    assert event.event_type == "ORDER_FILLED"
    assert json.loads(event.payload)["filled_quantity"] == 0.01
    projected = db.query(VenueOrder).one()
    assert projected.status == "FILLED"
    assert projected.filled_quantity == 0.01
    assert projected.remaining_quantity == 0.0
    assert projected.average_price == 3998.5
    assert db.query(ExecutionLeg).one().status == "FILLED"
    assert db.query(ExecutionIntent).one().status == "COMPLETED"


def test_projection_participates_in_callers_transaction() -> None:
    db = _session()
    order = _order(db)
    project_legacy_order(db, order)

    db.rollback()

    assert db.query(Order).count() == 0
    assert db.query(ExecutionEvent).count() == 0
    assert db.query(VenueOrder).count() == 0

