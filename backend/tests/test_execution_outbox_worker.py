"""执行 Outbox Worker 的可靠投递与恢复测试。"""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import Base, ExecutionEvent, ExecutionIntent, ExecutionLeg, ExecutionOutbox, Order, SystemLog, VenueOrder
from app.execution.intents import ExecutionLegPlan, create_execution_intent
from app.execution.outbox_worker import reconcile_execution_orders_once, run_execution_outbox_once
from tests.native_fakes import order_snapshot


class FakeAdapter:
    platform = "binance"

    def __init__(self, calls: list, *, query_result=None) -> None:
        self.calls = calls
        self.query_result = query_result or {"status": "not_ready"}

    def submit_order(self, order):
        self.calls.append(order)
        return order_snapshot(order, filled=float(order.quantity), price=4000, commission=0.01, venue_order_id="venue-99")

    def get_order(self, symbol, **kwargs):
        status = self.query_result.get("status", "unknown")
        return order_snapshot(venue="binance", symbol=symbol, status="unknown" if status == "not_ready" else status)


class PendingThenFilledAdapter:
    platform = "binance"

    def __init__(self, calls: list) -> None:
        self.calls = calls

    def submit_order(self, order):
        self.calls.append(order)
        return order_snapshot(order, status="submitted", venue_order_id="venue-pending")

    def get_order(self, symbol, **kwargs):
        return order_snapshot(venue="binance", symbol=symbol, requested=0.01, filled=0.01, price=4001, commission=0.01, venue_order_id="venue-pending")


class FailingAdapter:
    def __init__(self, *, outcome_unknown: bool = False) -> None:
        self.outcome_unknown = outcome_unknown

    def submit_order(self, order):
        error = RuntimeError("Binance 私有 WebSocket 尚未连接")
        error.outcome_unknown = self.outcome_unknown
        raise error


def _factory_and_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _create(factory) -> int:
    with factory() as db:
        result = create_execution_intent(
            db,
            intent_type="CLOSE",
            execution_mode="live",
            idempotency_key="close:42:v1",
            legs=[ExecutionLegPlan(
                leg_key="leg_a", venue="binance",
                instrument_id="XAUUSDT-PERP.BINANCE", venue_symbol="XAUUSDT",
                action="CLOSE", position_side="LONG", order_side="SELL",
                strategy_quantity=0.01, venue_order_quantity=0.01,
                venue_reduce_only=False,
            )],
        )
        db.commit()
        return result.intent.id


def test_worker_uses_stable_client_order_id_and_is_not_replayed() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)
    calls = []

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FakeAdapter(calls),
    ) == 1
    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FakeAdapter(calls),
    ) == 0

    assert len(calls) == 1
    assert calls[0].client_order_id.startswith("CH-")
    assert calls[0].position_side == "LONG"
    assert calls[0].reduce_only is False
    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "COMPLETED"
        assert db.query(ExecutionOutbox).one().status == "SENT"
        projected = db.query(VenueOrder).one()
        assert projected.status == "FILLED"
        assert projected.client_order_id == calls[0].client_order_id
        assert db.query(ExecutionEvent).one().event_type == "ORDER_FILLED"


def test_stale_processing_command_queries_and_never_resubmits() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)
    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        leg = intent and db.query(ExecutionLeg).filter_by(intent_id=intent.id).one()
        db.add(VenueOrder(
            execution_leg_id=leg.id,
            client_order_id=f"CH-{intent.id}-{leg.id}",
            status="INITIALIZED", requested_quantity=0.01,
            filled_quantity=0.0, remaining_quantity=0.01,
        ))
        outbox = db.query(ExecutionOutbox).one()
        outbox.status = "PROCESSING"
        outbox.locked_at = utc_now() - timedelta(seconds=60)
        db.commit()
    calls = []

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FakeAdapter(calls, query_result={"status": "not_ready"}),
        processing_timeout_seconds=30,
    ) == 1

    assert calls == []
    with factory() as db:
        assert db.query(ExecutionOutbox).one().status == "FAILED"
        assert db.get(ExecutionIntent, intent_id).status == "RECOVERY_REQUIRED"
        assert "禁止自动重发" in db.query(ExecutionOutbox).one().last_error


def test_sent_order_is_not_polled_and_explicit_recovery_does_not_resubmit() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)
    calls = []
    adapter = PendingThenFilledAdapter(calls)

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: adapter,
    ) == 1
    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "RUNNING"
        assert db.query(VenueOrder).one().status == "SUBMITTED"

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: adapter,
    ) == 0

    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "RUNNING"
    reconcile_execution_orders_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: adapter,
    )

    assert len(calls) == 1
    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "COMPLETED"
        assert db.query(VenueOrder).one().status == "FILLED"
        assert db.query(ExecutionEvent).count() == 2


def test_deterministic_submit_failure_is_persisted_everywhere() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FailingAdapter(),
    ) == 1

    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        order = db.query(Order).one()
        venue_order = db.query(VenueOrder).one()
        outbox = db.query(ExecutionOutbox).one()
        event = db.query(ExecutionEvent).one()
        system_log = db.query(SystemLog).one()
        assert intent.status == "FAILED"
        assert "私有 WebSocket 尚未连接" in intent.error_message
        assert order.status == "failed"
        assert order.error_message == intent.error_message
        assert venue_order.status == "REJECTED"
        assert outbox.status == "SENT"
        assert event.event_type == "ORDER_REJECTED"
        assert "私有 WebSocket 尚未连接" in event.payload
        assert system_log.category == "execution"
        assert '"outcome_unknown":false' in system_log.context


def test_unknown_submit_failure_keeps_recovery_state_and_records_reason() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FailingAdapter(outcome_unknown=True),
    ) == 1

    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        order = db.query(Order).one()
        venue_order = db.query(VenueOrder).one()
        outbox = db.query(ExecutionOutbox).one()
        assert intent.status == "RECOVERY_REQUIRED"
        assert "私有 WebSocket 尚未连接" in intent.error_message
        assert order.status == "unknown"
        assert order.error_message
        assert venue_order.status == "UNKNOWN"
        assert outbox.status == "PROCESSING"
        assert "提交结果未知" in outbox.last_error
        assert db.query(SystemLog).filter_by(category="execution").count() == 1
