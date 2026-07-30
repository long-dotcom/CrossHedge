"""执行 Outbox Worker 的可靠投递与恢复测试。"""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import Base, ExecutionEvent, ExecutionIntent, ExecutionLeg, ExecutionOutbox, HedgeGroup, Order, SystemLog, VenueOrder
from app.execution.intents import ExecutionLegPlan, create_execution_intent
from app.execution.outbox_worker import (
    reconcile_execution_orders_once,
    repair_stale_predispatch_intents_once,
    repair_stale_unsent_outboxes_once,
    run_execution_outbox_once,
)
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


class QueryMustNotRunAdapter:
    def get_order(self, symbol, **kwargs):
        raise AssertionError("确定性历史拒单不应再次查询场所")


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


def test_historical_mt5_market_closed_unknown_converges_without_query() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)
    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        leg = db.query(ExecutionLeg).filter_by(intent_id=intent.id).one()
        leg.venue = "mt5"
        leg.venue_symbol = "XAUUSD"
        order = Order(
            platform="mt5", symbol="GOLD", side="sell", quantity=0.01,
            order_type="market", status="unknown",
        )
        db.add(order)
        db.flush()
        db.add(VenueOrder(
            execution_leg_id=leg.id, legacy_order_id=order.id,
            client_order_id=f"CH-{intent.id}-{leg.id}", venue_order_id="",
            status="UNKNOWN", requested_quantity=0.01, filled_quantity=0,
            remaining_quantity=0.01, reconciliation_state="SUBMIT_UNKNOWN",
            raw_last_report='{"error_message":"RuntimeError: MT5 下单失败 retcode=10018: Market closed","outcome_unknown":true}',
        ))
        outbox = db.query(ExecutionOutbox).one()
        outbox.status = "PROCESSING"
        outbox.locked_at = utc_now() - timedelta(seconds=60)
        db.commit()

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: QueryMustNotRunAdapter(),
        processing_timeout_seconds=30,
    ) == 1
    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: QueryMustNotRunAdapter(),
        processing_timeout_seconds=30,
    ) == 0

    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "FAILED"
        assert db.query(ExecutionLeg).one().status == "FAILED"
        assert db.query(VenueOrder).one().status == "REJECTED"
        assert db.query(VenueOrder).one().reconciliation_state == "RECOVERED_SUBMIT_REJECTION"
        assert db.query(ExecutionOutbox).one().status == "SENT"
        assert db.query(Order).one().status == "rejected"
        assert db.query(ExecutionEvent).filter_by(event_type="ORDER_REJECTED").count() == 1


def test_stale_sent_planned_outbox_without_external_facts_is_requeued() -> None:
    factory = _factory_and_session()
    intent_id = _create(factory)
    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        intent.status = "RUNNING"
        outbox = db.query(ExecutionOutbox).one()
        leg = db.query(ExecutionLeg).filter_by(intent_id=intent_id).one()
        outbox.payload = '{"dispatch_leg_ids":[%d]}' % leg.id
        outbox.status = "SENT"
        outbox.created_at = utc_now() - timedelta(minutes=10)
        db.commit()

    assert repair_stale_unsent_outboxes_once(
        session_factory=factory, stale_after_seconds=60,
    ) == 1
    with factory() as db:
        outbox = db.query(ExecutionOutbox).one()
        assert outbox.status == "PENDING"
        assert "安全重新排队" in outbox.last_error
        assert db.get(ExecutionIntent, intent_id).status == "RUNNING"


def test_stale_predispatch_close_is_rolled_back_only_without_external_facts() -> None:
    factory = _factory_and_session()
    with factory() as db:
        group = HedgeGroup(
            symbol="GOLD", direction="long_leg_b_short_leg_a", status="closing",
            execution_mode="paper", notional=4000, quantity=1,
            leg_a_quantity=1, leg_b_quantity=0.01,
        )
        db.add(group)
        db.flush()
        result = create_execution_intent(
            db,
            intent_type="CLOSE",
            execution_mode="paper",
            execution_style="maker_then_market",
            idempotency_key="stale-close-without-submit",
            hedge_group_id=group.id,
            legs=[ExecutionLegPlan(
                leg_key="leg_a", venue="binance", instrument_id="XAUUSDT",
                venue_symbol="XAUUSDT", action="CLOSE", position_side="SHORT",
                order_side="BUY", strategy_quantity=1, venue_order_quantity=1,
                order_type="limit", post_only=True, role="MAKER",
            )],
            command_payload={"previous_group_status": "open"},
        )
        result.intent.created_at = utc_now() - timedelta(minutes=10)
        intent_id = result.intent.id
        group_id = group.id
        db.commit()

    assert repair_stale_predispatch_intents_once(
        session_factory=factory, stale_after_seconds=60,
    ) == 1

    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "FAILED"
        assert db.get(HedgeGroup, group_id).status == "open"
        assert db.query(ExecutionOutbox).one().status == "CANCELED"
        assert db.query(VenueOrder).count() == 0

        protected = HedgeGroup(
            symbol="GOLD", direction="long_leg_b_short_leg_a", status="closing",
            execution_mode="paper", notional=4000, quantity=1,
            leg_a_quantity=1, leg_b_quantity=0.01,
        )
        db.add(protected)
        db.flush()
        protected_result = create_execution_intent(
            db,
            intent_type="CLOSE", execution_mode="paper",
            idempotency_key="stale-close-with-external-fact", hedge_group_id=protected.id,
            legs=[ExecutionLegPlan(
                leg_key="leg_a", venue="binance", instrument_id="XAUUSDT",
                venue_symbol="XAUUSDT", action="CLOSE", position_side="SHORT",
                order_side="BUY", strategy_quantity=1, venue_order_quantity=1,
            )],
            command_payload={"previous_group_status": "open"},
        )
        protected_result.intent.created_at = utc_now() - timedelta(minutes=10)
        protected_leg = db.query(ExecutionLeg).filter_by(intent_id=protected_result.intent.id).one()
        db.add(VenueOrder(
            execution_leg_id=protected_leg.id, client_order_id="protected-order",
            status="INITIALIZED", requested_quantity=1, filled_quantity=0,
            remaining_quantity=1,
        ))
        protected_id = protected.id
        protected_intent_id = protected_result.intent.id
        db.commit()

    assert repair_stale_predispatch_intents_once(
        session_factory=factory, stale_after_seconds=60,
    ) == 0
    with factory() as db:
        assert db.get(ExecutionIntent, protected_intent_id).status == "CREATED"
        assert db.get(HedgeGroup, protected_id).status == "closing"
