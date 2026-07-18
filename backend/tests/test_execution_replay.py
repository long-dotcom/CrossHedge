"""不可变执行事件灾难恢复测试。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ExecutionLeg, HedgeGroup, SymbolMapping, VenueOrder
from app.execution.coordinator import create_close_intent
from app.execution.outbox_worker import run_execution_outbox_once
from app.execution.replay import rebuild_intent_projection
from app.schemas import ReplayExecutionIntentIn
from tests.native_fakes import order_snapshot


class FillAdapter:
    def __init__(self, venue: str) -> None:
        self.platform = venue

    def submit_order(self, order):
        return order_snapshot(order, venue=self.platform, filled=float(order.quantity), price=100, commission=0.01, venue_order_id=f"{self.platform}-venue-id")

    def get_order(self, symbol, **kwargs):
        return order_snapshot(venue=self.platform, symbol=symbol, status="filled")


def test_completed_close_can_be_rebuilt_from_immutable_events() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as db:
        db.add(SymbolMapping(
            symbol="GOLD", leg_a_venue="binance", leg_a_symbol="XAUUSDT",
            leg_a_venue_symbol="XAUUSDT", leg_b_venue="mt5",
            leg_b_symbol="XAUUSD", mt5_symbol="XAUUSD",
        ))
        group = HedgeGroup(
            symbol="GOLD", direction="long_leg_a_short_leg_b", status="open",
            execution_mode="paper", notional=4000, quantity=1,
            leg_a_quantity=0.001, leg_b_quantity=0.01,
        )
        db.add(group)
        db.commit()
        result = create_close_intent(
            db, group_id=group.id, reason="replay test", requested_by="test",
            idempotency_key="close:replay:1",
        )
        intent_id = result.intent.id
        group_id = group.id
        db.commit()

    run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FillAdapter(venue),
    )
    with factory() as db:
        orders = db.query(VenueOrder).join(ExecutionLeg).filter(ExecutionLeg.intent_id == intent_id).all()
        assert db.get(HedgeGroup, group_id).status == "closed"
        for order in orders:
            order.status = "UNKNOWN"
            order.filled_quantity = 0
            order.remaining_quantity = order.requested_quantity
            order.average_price = None
        for leg in db.query(ExecutionLeg).filter_by(intent_id=intent_id):
            leg.status = "UNKNOWN"
        db.get(HedgeGroup, group_id).status = "manual_intervention"
        db.commit()

        replayed = rebuild_intent_projection(db, intent_id)
        db.commit()

        assert replayed.event_count == 2
        assert replayed.intent_status == "COMPLETED"
        assert db.get(HedgeGroup, group_id).status == "closed"
        rebuilt = db.query(VenueOrder).join(ExecutionLeg).filter(ExecutionLeg.intent_id == intent_id).all()
        assert all(order.status == "FILLED" and order.filled_quantity > 0 for order in rebuilt)


def test_admin_replay_endpoint_requires_exact_confirmation_and_creates_no_commands(monkeypatch) -> None:
    from app.api.execution import replay_execution_intent

    calls: list[tuple] = []
    db = SimpleNamespace(commit=lambda: calls.append(("commit",)))
    user = SimpleNamespace(id=7)
    monkeypatch.setattr(
        "app.api.execution.rebuild_intent_projection",
        lambda session, intent_id: SimpleNamespace(
            intent_id=intent_id,
            event_count=4,
            order_count=2,
            intent_status="COMPLETED",
        ),
    )
    monkeypatch.setattr("app.api.execution.audit", lambda *args: calls.append(("audit", *args[1:])))

    with pytest.raises(HTTPException, match="REPLAY 42"):
        replay_execution_intent(42, ReplayExecutionIntentIn(confirmation="REPLAY 41"), user, db)

    result = replay_execution_intent(42, ReplayExecutionIntentIn(confirmation=" REPLAY 42 "), user, db)

    assert result == {
        "status": "ok",
        "intent_id": 42,
        "event_count": 4,
        "order_count": 2,
        "intent_status": "COMPLETED",
        "external_commands_created": 0,
    }
    assert [row[0] for row in calls] == ["audit", "commit"]
