"""持久化 Maker-then-Market、TTL 和补偿状态机测试。"""

from dataclasses import asdict
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import Base, ExecutionIntent, ExecutionLeg, ExecutionOutbox, HedgeGroup, HedgeGroupEvent, SymbolMapping, VenueOrder
from app.execution.intents import ExecutionLegPlan, create_execution_intent
from app.execution.outbox_worker import reconcile_execution_orders_once, run_execution_outbox_once
from tests.native_fakes import order_snapshot


class MakerAdapter:
    def __init__(self, venue: str, state: dict) -> None:
        self.platform = venue
        self.venue = venue
        self.state = state

    def submit_order(self, order):
        self.state.setdefault("placed", []).append((self.venue, order))
        if order.post_only:
            return order_snapshot(order, venue=self.venue, status="submitted", venue_order_id="maker-venue-1")
        if self.venue == "mt5":
            if self.state.get("reject_hedge"):
                return order_snapshot(order, venue=self.venue, status="rejected", venue_order_id="hedge-rejected")
            return order_snapshot(order, venue=self.venue, filled=float(order.quantity), price=200, commission=0.01, venue_order_id="hedge-filled")
        return order_snapshot(order, venue=self.venue, filled=float(order.quantity), price=100, commission=0.01, venue_order_id="maker-market-filled")

    def get_order(self, symbol, **kwargs):
        if self.state.get("cancel_requested"):
            return order_snapshot(
                venue=self.venue, symbol=symbol, status="canceled", venue_order_id="maker-venue-1",
                requested=0.01, filled=float(self.state.get("maker_fill", 0.004)), price=100, commission=0.001,
            )
        return order_snapshot(venue=self.venue, symbol=symbol, status="accepted", venue_order_id="maker-venue-1", requested=0.01)

    def cancel_order(self, symbol, **kwargs):
        self.state["cancel_requested"] = True
        self.state.setdefault("canceled", []).append(kwargs.get("venue_order_id") or kwargs.get("client_order_id"))
        return order_snapshot(venue=self.venue, symbol=symbol, status="pending_cancel", venue_order_id="maker-venue-1", requested=0.01)


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _create_maker_open(factory, *, unfilled_action="cancel", single_leg_action="manual_intervention") -> int:
    maker = ExecutionLegPlan(
        leg_key="leg_a", role="MAKER", sequence=0,
        venue="binance", instrument_id="BTCUSDT-PERP.BINANCE", venue_symbol="BTCUSDT",
        action="OPEN", position_side="LONG", order_side="BUY",
        strategy_quantity=0.01, venue_order_quantity=0.01,
        order_type="limit", limit_price=100.0, post_only=True,
    )
    hedge = ExecutionLegPlan(
        leg_key="leg_b", role="HEDGE", sequence=1,
        venue="mt5", instrument_id="BTCUSD", venue_symbol="BTCUSD",
        action="OPEN", position_side="SHORT", order_side="SELL",
        strategy_quantity=0.02, venue_order_quantity=0.02,
        order_type="market",
    )
    with factory() as db:
        db.add(SymbolMapping(
            symbol="BTC", leg_a_venue="binance", leg_a_symbol="BTCUSDT",
            leg_a_venue_symbol="BTCUSDT", leg_b_venue="mt5",
            leg_b_symbol="BTCUSD", mt5_symbol="BTCUSD",
        ))
        group = HedgeGroup(
            symbol="BTC", direction="long_leg_a_short_leg_b", status="opening",
            execution_mode="live", notional=100.0, quantity=0.01,
            leg_a_quantity=0.01, leg_b_quantity=0.02,
        )
        db.add(group)
        db.flush()
        result = create_execution_intent(
            db,
            intent_type="OPEN",
            execution_mode="live",
            execution_style="maker_then_market",
            idempotency_key=f"maker-open-{unfilled_action}-{single_leg_action}",
            hedge_group_id=group.id,
            legs=[maker],
            command_payload={
                "maker_state_version": 1,
                "maker_target_quantity": 0.01,
                "hedge_target_quantity": 0.02,
                "hedge_template": asdict(hedge),
                "maker_ttl_seconds": 1,
                "maker_unfilled_action": unfilled_action,
                "single_leg_action": single_leg_action,
            },
        )
        db.commit()
        return result.intent.id


def _create_maker_close(factory, *, group_id: int | None = None, suffix: str) -> tuple[int, int]:
    maker = ExecutionLegPlan(
        leg_key="leg_a", role="MAKER", sequence=0,
        venue="binance", instrument_id="BTCUSDT-PERP.BINANCE", venue_symbol="BTCUSDT",
        action="CLOSE", position_side="LONG", order_side="SELL",
        strategy_quantity=0.01, venue_order_quantity=0.01,
        order_type="limit", limit_price=100.0, post_only=True, venue_reduce_only=True,
    )
    hedge = ExecutionLegPlan(
        leg_key="leg_b", role="HEDGE", sequence=1,
        venue="mt5", instrument_id="BTCUSD", venue_symbol="BTCUSD",
        action="CLOSE", position_side="SHORT", order_side="BUY",
        strategy_quantity=0.02, venue_order_quantity=0.02,
        order_type="market", venue_reduce_only=True,
    )
    with factory() as db:
        group = db.get(HedgeGroup, group_id) if group_id is not None else None
        if group is None:
            group = HedgeGroup(
                symbol="BTC", direction="long_leg_a_short_leg_b", status="open",
                execution_mode="live", notional=100.0, quantity=0.01,
                leg_a_quantity=0.01, leg_b_quantity=0.02,
            )
            db.add(group)
            db.flush()
        result = create_execution_intent(
            db,
            intent_type="CLOSE",
            execution_mode="live",
            execution_style="maker_then_market",
            idempotency_key=f"maker-close-{suffix}",
            hedge_group_id=group.id,
            legs=[maker],
            command_payload={
                "maker_state_version": 1,
                "maker_target_quantity": 0.01,
                "hedge_target_quantity": 0.02,
                "hedge_template": asdict(hedge),
                "maker_ttl_seconds": 1,
                "maker_unfilled_action": "cancel",
                "previous_group_status": "open",
                "reason": "test close",
            },
        )
        group.status = "closing"
        db.commit()
        return result.intent.id, group.id


def test_ttl_cancel_waits_for_terminal_then_hedges_partial_fill() -> None:
    factory = _session_factory()
    intent_id = _create_maker_open(factory)
    state = {"maker_fill": 0.004}
    adapter_factory = lambda venue, mode: MakerAdapter(venue, state)

    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        maker = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="MAKER").one()
        maker.created_at = utc_now() - timedelta(seconds=2)
        db.commit()

    # 第一轮到期先领取持久化撤单 Outbox，尚未确认 CANCELED 时不得创建 Hedge/Fallback。
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        assert db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="HEDGE").count() == 0
        assert db.query(VenueOrder).one().status == "PENDING_CANCEL"
        assert state["canceled"] == ["maker-venue-1"]

    # 模拟私有流重连后的单次补偿确认，随后按比例只提交 0.008 Hedge。
    reconcile_execution_orders_once(session_factory=factory, adapter_factory=adapter_factory)
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        group = db.get(HedgeGroup, intent.hedge_group_id)
        hedge = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="HEDGE").one()
        assert hedge.venue_order_quantity == 0.008
        assert intent.status == "COMPLETED"
        assert group.status == "open_partial"
        assert group.leg_a_quantity == 0.004
        assert group.leg_b_quantity == 0.008
        assert group.entry_spread == 100
    assert len(state["placed"]) == 2


def test_market_fallback_is_enqueued_only_after_cancel_confirmation() -> None:
    factory = _session_factory()
    intent_id = _create_maker_open(factory, unfilled_action="market_fallback")
    state = {"maker_fill": 0.0}
    adapter_factory = lambda venue, mode: MakerAdapter(venue, state)

    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        maker = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="MAKER").one()
        maker.created_at = utc_now() - timedelta(seconds=2)
        db.commit()
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        assert db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="MAKER_FALLBACK").count() == 0

    reconcile_execution_orders_once(session_factory=factory, adapter_factory=adapter_factory)
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        fallback = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="MAKER_FALLBACK").one()
        assert fallback.venue_order_quantity == 0.01
        assert fallback.post_only is False
        # Fallback 成交后已创建增量 Hedge，下一轮才领取该独立命令。
        assert db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="HEDGE").count() == 1
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        assert db.get(ExecutionIntent, intent_id).status == "COMPLETED"


def test_maker_rejection_preserves_venue_error_instead_of_ttl_message() -> None:
    factory = _session_factory()
    intent_id = _create_maker_open(factory)
    state = {"maker_fill": 0.0}

    class RejectedMakerAdapter(MakerAdapter):
        def submit_order(self, order):
            self.state.setdefault("placed", []).append((self.venue, order))
            return order_snapshot(
                order,
                venue=self.venue,
                status="rejected",
                error_message="Paper Post-only 订单会立即成交，模拟器拒绝挂单",
            )

    run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: RejectedMakerAdapter(venue, state),
    )

    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        group = db.get(HedgeGroup, intent.hedge_group_id)
        venue_order = db.query(VenueOrder).one()
        assert intent.status == "FAILED"
        assert intent.error_message == "Paper Post-only 订单会立即成交，模拟器拒绝挂单"
        assert group.close_reason == intent.error_message
        assert venue_order.status == "REJECTED"


def test_repeated_maker_close_rejections_finish_each_intent_independently() -> None:
    factory = _session_factory()
    state = {"maker_fill": 0.0}

    class RejectedCloseAdapter(MakerAdapter):
        def submit_order(self, order):
            self.state.setdefault("placed", []).append((self.venue, order))
            return order_snapshot(
                order,
                venue=self.venue,
                status="rejected",
                error_message="真实探针未成交",
            )

    adapter_factory = lambda venue, mode: RejectedCloseAdapter(venue, state)
    first_intent_id, group_id = _create_maker_close(factory, suffix="first")
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1

    second_intent_id, _ = _create_maker_close(factory, group_id=group_id, suffix="second")
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1

    with factory() as db:
        first = db.get(ExecutionIntent, first_intent_id)
        second = db.get(ExecutionIntent, second_intent_id)
        group = db.get(HedgeGroup, group_id)
        events = db.query(HedgeGroupEvent).filter_by(
            hedge_group_id=group_id,
            event_type="maker_close_completed",
        ).all()

        assert first.status == "FAILED"
        assert second.status == "FAILED"
        assert group.status == "open"
        assert len(events) == 2
        assert {event.detail.split(";")[0] for event in events} == {
            f"Intent #{first_intent_id}",
            f"Intent #{second_intent_id}",
        }


def test_failed_hedge_creates_separate_flatten_compensation() -> None:
    factory = _session_factory()
    intent_id = _create_maker_open(
        factory,
        unfilled_action="market_fallback",
        single_leg_action="auto_close",
    )
    state = {"maker_fill": 0.0, "reject_hedge": True}
    adapter_factory = lambda venue, mode: MakerAdapter(venue, state)

    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        maker = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="MAKER").one()
        maker.created_at = utc_now() - timedelta(seconds=2)
        db.commit()
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    reconcile_execution_orders_once(session_factory=factory, adapter_factory=adapter_factory)
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    # Hedge 动态命令被拒绝后，应生成独立反向补偿腿。
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        compensation = db.query(ExecutionLeg).filter_by(intent_id=intent_id, role="COMPENSATION").one()
        assert compensation.order_side == "SELL"
        assert compensation.position_side == "LONG"
        assert compensation.venue_reduce_only is False
        assert compensation.venue_order_quantity == 0.01
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        intent = db.get(ExecutionIntent, intent_id)
        group = db.get(HedgeGroup, intent.hedge_group_id)
        assert intent.status == "FAILED"
        assert group.status == "failed"
