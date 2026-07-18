"""下一代执行意图与 Outbox 事务语义测试。"""

import json
from dataclasses import replace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ExecutionIntent, ExecutionLeg, ExecutionOutbox
from app.execution.intents import ExecutionLegPlan, create_execution_intent


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _leg(key: str = "leg_a") -> ExecutionLegPlan:
    return ExecutionLegPlan(
        leg_key=key,
        venue="binance",
        instrument_id="XAUUSDT-PERP.BINANCE",
        venue_symbol="XAUUSDT",
        action="OPEN",
        position_side="LONG",
        order_side="BUY",
        strategy_quantity=0.01,
        venue_order_quantity=0.01,
        venue_reduce_only=False,
    )


def test_create_intent_writes_leg_and_outbox_without_committing(db) -> None:
    result = create_execution_intent(
        db,
        intent_type="open",
        idempotency_key="group:42:open:v1",
        hedge_group_id=None,
        legs=[_leg()],
        command_payload={"source": "api"},
    )

    assert result.created is True
    assert db.query(ExecutionIntent).count() == 1
    assert db.query(ExecutionLeg).count() == 1
    outbox = db.query(ExecutionOutbox).one()
    assert outbox.status == "PENDING"
    payload = json.loads(outbox.payload)
    assert payload["intent_id"] == result.intent.id
    assert payload["source"] == "api"

    db.rollback()
    assert db.query(ExecutionIntent).count() == 0
    assert db.query(ExecutionLeg).count() == 0
    assert db.query(ExecutionOutbox).count() == 0


def test_same_idempotency_key_reuses_existing_intent(db) -> None:
    first = create_execution_intent(
        db,
        intent_type="OPEN",
        idempotency_key="group:42:open:v1",
        legs=[_leg()],
    )
    second = create_execution_intent(
        db,
        intent_type="OPEN",
        idempotency_key="group:42:open:v1",
        legs=[_leg()],
    )

    assert first.created is True
    assert second.created is False
    assert second.intent.id == first.intent.id
    assert db.query(ExecutionIntent).count() == 1
    assert db.query(ExecutionLeg).count() == 1
    assert db.query(ExecutionOutbox).count() == 1


def test_invalid_leg_plan_is_rejected_before_writing(db) -> None:
    with pytest.raises(ValueError, match="数量必须大于 0"):
        create_execution_intent(
            db,
            intent_type="OPEN",
            idempotency_key="invalid",
            legs=[replace(_leg(), venue_order_quantity=0)],
        )

    assert db.query(ExecutionIntent).count() == 0
