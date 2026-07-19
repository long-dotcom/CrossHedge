"""对冲组安全作废归档测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base,
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
from app.execution.voiding import void_eligibility, void_hedge_group


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def _group(db, *, profile: str = "legacy_local", status: str = "opening") -> HedgeGroup:
    row = HedgeGroup(
        symbol="BTC",
        direction="long_leg_b_short_leg_a",
        status=status,
        execution_mode="paper",
        execution_profile=profile,
        notional=640,
        quantity=0.01,
    )
    db.add(row)
    db.flush()
    return row


def _intent_and_leg(db, group: HedgeGroup, *, leg_status: str = "PLANNED") -> tuple[ExecutionIntent, ExecutionLeg]:
    intent = ExecutionIntent(
        hedge_group_id=group.id,
        intent_type="OPEN",
        execution_mode="paper",
        idempotency_key=f"void-test-{group.id}",
        status="RUNNING",
    )
    db.add(intent)
    db.flush()
    leg = ExecutionLeg(
        intent_id=intent.id,
        leg_key="leg_a",
        venue="hyperliquid",
        instrument_id="BTC",
        venue_symbol="BTC",
        action="OPEN",
        position_side="SHORT",
        order_side="SELL",
        strategy_quantity=0.01,
        venue_order_quantity=0.01,
        status=leg_status,
    )
    db.add(leg)
    db.flush()
    return intent, leg


def test_legacy_local_stuck_group_can_be_voided_without_deleting_audit_facts() -> None:
    db = _db()
    group = _group(db)
    intent, filled_leg = _intent_and_leg(db, group, leg_status="FILLED")
    pending_leg = ExecutionLeg(
        intent_id=intent.id,
        leg_key="leg_b",
        venue="mt5",
        instrument_id="BTCUSD",
        venue_symbol="BTCUSD",
        action="OPEN",
        position_side="LONG",
        order_side="BUY",
        strategy_quantity=0.01,
        venue_order_quantity=0.01,
        status="PLANNED",
    )
    db.add(pending_leg)
    db.flush()
    db.add_all([
        VenueOrder(
            execution_leg_id=filled_leg.id,
            client_order_id="CH-1-1",
            venue_order_id="paper-hyperliquid-1",
            status="FILLED",
            requested_quantity=0.01,
            filled_quantity=0.01,
            remaining_quantity=0,
        ),
        VenueOrder(
            execution_leg_id=pending_leg.id,
            client_order_id="CH-1-2",
            status="INITIALIZED",
            requested_quantity=0.01,
            filled_quantity=0,
            remaining_quantity=0.01,
        ),
    ])
    local_order = Order(
        hedge_group_id=group.id,
        platform="hyperliquid",
        symbol="BTC",
        side="sell",
        quantity=0.01,
        status="filled",
        external_order_id="paper-hyperliquid-1",
    )
    pending_order = Order(
        hedge_group_id=group.id,
        platform="mt5",
        symbol="BTC",
        side="buy",
        quantity=0.01,
        status="initialized",
    )
    db.add_all([local_order, pending_order])
    db.flush()
    db.add(Fill(order_id=local_order.id, platform="hyperliquid", symbol="BTC", side="sell", quantity=0.01, price=64000))
    db.add(ExecutionOutbox(intent_id=intent.id, event_type="EXECUTION_INTENT_CREATED", status="SENT"))
    db.commit()

    assert void_eligibility(db, group).allowed is True
    result = void_hedge_group(db, group.id, reason="旧版 Paper 行情错误", requested_by="user:1")
    db.commit()

    assert result.status == "voided"
    assert result.closed_at is not None
    assert db.get(ExecutionIntent, intent.id).status == "VOIDED"
    assert db.get(ExecutionLeg, pending_leg.id).status == "CANCELED"
    assert db.query(VenueOrder).filter(VenueOrder.client_order_id == "CH-1-2").one().status == "CANCELED"
    assert db.get(Order, pending_order.id).status == "canceled"
    assert db.query(Fill).count() == 1
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "voided").count() == 1


def test_group_with_real_fill_cannot_be_voided() -> None:
    db = _db()
    group = _group(db, profile="hybrid_probe_demo", status="manual_intervention")
    _, leg = _intent_and_leg(db, group, leg_status="FILLED")
    db.add(VenueOrder(
        execution_leg_id=leg.id,
        client_order_id="CH-real",
        venue_order_id="2165679674",
        status="FILLED",
        requested_quantity=0.01,
        filled_quantity=0.01,
        remaining_quantity=0,
    ))
    db.commit()

    decision = void_eligibility(db, group)
    assert decision.allowed is False
    assert "真实成交" in decision.reason


def test_group_with_unknown_order_or_unflattened_probe_cannot_be_voided() -> None:
    db = _db()
    unknown_group = _group(db, profile="hybrid_probe_demo")
    _, leg = _intent_and_leg(db, unknown_group)
    db.add(VenueOrder(
        execution_leg_id=leg.id,
        client_order_id="CH-unknown",
        status="UNKNOWN",
        requested_quantity=0.01,
        filled_quantity=0,
        remaining_quantity=0.01,
    ))
    probe_group = _group(db, profile="hybrid_probe_demo")
    db.add(ProbeRun(
        hedge_group_id=probe_group.id,
        purpose="PAPER_OPEN",
        venue="hyperliquid",
        instrument_id="ETH",
        probe_quantity=0.01,
        status="RECOVERY_REQUIRED",
    ))
    db.commit()

    assert void_eligibility(db, unknown_group).allowed is False
    probe_decision = void_eligibility(db, probe_group)
    assert probe_decision.allowed is False
    assert "真实探针" in probe_decision.reason
