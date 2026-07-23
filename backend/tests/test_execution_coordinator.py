"""异步平仓 Coordinator 与对冲组状态投影测试。"""

import json

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import ArbitrageOpportunity, Base, ExecutionIntent, ExecutionLeg, ExecutionOutbox, Fill, HedgeGroup, Order, StrategySetting, SymbolMapping
from app.execution.coordinator import create_close_intent, create_open_intent, create_recovery_intent
from app.execution.outbox_worker import run_execution_outbox_once
from app.execution.pnl import actual_entry_spread_from_fills
from app.execution.preflight import refreshed_opportunity_still_executable
from tests.native_fakes import order_snapshot


class FillingAdapter:
    def __init__(self, venue: str, calls: list) -> None:
        self.platform = venue
        self.calls = calls

    def submit_order(self, order):
        self.calls.append(order)
        price = 4000.0 if self.platform == "binance" else 3999.0
        return order_snapshot(order, venue=self.platform, filled=float(order.quantity), price=price, commission=0.01, venue_order_id=f"{self.platform}-filled")

    def get_order(self, symbol, **kwargs):
        return order_snapshot(venue=self.platform, symbol=symbol, requested=0.01, filled=0.01)


class OneLegRejectedAdapter(FillingAdapter):
    def submit_order(self, order):
        self.calls.append(order)
        if self.platform == "mt5":
            return order_snapshot(order, venue=self.platform, status="rejected")
        return order_snapshot(order, venue=self.platform, filled=float(order.quantity), price=4000, commission=0.01, venue_order_id="binance-filled")


def _factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _group_and_mapping(db, *, mode: str) -> HedgeGroup:
    db.add(SymbolMapping(
        symbol="GOLD",
        leg_a_venue="binance",
        leg_a_venue_symbol="XAUUSDT",
        leg_a_symbol="XAUUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="XAUUSD",
        mt5_symbol="XAUUSD",
    ))
    group = HedgeGroup(
        symbol="GOLD",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode=mode,
        notional=4000,
        quantity=1.0,
        leg_a_quantity=0.002,
        leg_b_quantity=0.03,
    )
    db.add(group)
    db.commit()
    return group


def _opening_fill(db, group: HedgeGroup, venue: str, side: str, quantity: float, price: float) -> None:
    order = Order(
        hedge_group_id=group.id, platform=venue, symbol=group.symbol,
        side=side, quantity=quantity, reduce_only=False, status="filled",
        external_order_id=f"{venue}-open",
    )
    db.add(order)
    db.flush()
    db.add(Fill(
        order_id=order.id, platform=venue, symbol=group.symbol,
        side=side, quantity=quantity, price=price, fee=0.01,
    ))


def test_live_close_plan_uses_attributed_residual_and_binance_position_side() -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="live")
        _opening_fill(db, group, "binance", "buy", 0.002, 4000)
        _opening_fill(db, group, "mt5", "sell", 0.03, 3999)
        db.commit()

        result = create_close_intent(
            db, group_id=group.id, reason="manual", requested_by="test",
            idempotency_key="close:gold:1",
        )
        db.commit()
        legs = db.query(ExecutionLeg).filter_by(intent_id=result.intent.id).order_by(ExecutionLeg.leg_key).all()

        assert group.status == "closing"
        assert [(leg.venue, leg.order_side, leg.position_side, leg.venue_order_quantity) for leg in legs] == [
            ("binance", "SELL", "LONG", 0.002),
            ("mt5", "BUY", "SHORT", 0.03),
        ]
        assert legs[0].venue_reduce_only is False
        assert legs[1].venue_reduce_only is True

        repeated = create_close_intent(
            db, group_id=group.id, reason="manual", requested_by="test",
            idempotency_key="close:gold:1",
        )
        assert repeated.created is False
        assert repeated.intent.id == result.intent.id


def test_paper_close_runs_as_async_intent_and_closes_only_after_both_fills() -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="paper")
        group_id = group.id
        create_close_intent(
            db, group_id=group_id, reason="manual async", requested_by="test",
            idempotency_key="close:paper:1",
        )
        db.commit()
        assert db.get(HedgeGroup, group_id).status == "closing"
    calls = []

    assert run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FillingAdapter(venue, calls),
    ) == 1

    assert len(calls) == 2
    assert all(order.reduce_only == (order.venue != "binance") for order in calls)
    with factory() as db:
        group = db.get(HedgeGroup, group_id)
        assert group.status == "closed"
        assert group.closed_at is not None
        assert group.fees == pytest.approx(0.02)
        close_orders = db.query(Order).filter_by(hedge_group_id=group_id, reduce_only=True).all()
        assert len(close_orders) == 2
        assert db.query(Fill).join(Order, Fill.order_id == Order.id).filter(Order.hedge_group_id == group_id).count() == 2


def test_single_leg_close_fill_enters_manual_recovery_instead_of_false_closed() -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="paper")
        group_id = group.id
        create_close_intent(
            db, group_id=group_id, reason="partial failure", requested_by="test",
            idempotency_key="close:paper:partial",
        )
        db.commit()
    calls = []

    run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: OneLegRejectedAdapter(venue, calls),
    )

    with factory() as db:
        group = db.get(HedgeGroup, group_id)
        assert group.status == "manual_intervention"
        assert "恢复计划" in group.close_reason
        assert db.query(Fill).join(Order, Fill.order_id == Order.id).filter(Order.hedge_group_id == group_id).count() == 1


def test_open_request_creates_intent_and_only_opens_after_two_confirmed_fills(monkeypatch) -> None:
    factory = _factory()
    with factory() as db:
        db.add(StrategySetting(
            execution_mode="paper", paper_use_live_account_risk=False,
            min_net_profit=0.0, min_total_profit=0.0,
        ))
        db.add(SymbolMapping(
            symbol="GOLD", leg_a_venue="binance", leg_a_venue_symbol="XAUUSDT", leg_a_symbol="XAUUSDT",
            leg_b_venue="mt5", leg_b_symbol="XAUUSD", mt5_symbol="XAUUSD",
            execution_style="simultaneous_market",
        ))
        opportunity = ArbitrageOpportunity(
            symbol="GOLD", direction="long_leg_a_short_leg_b", status="executable",
            notional=4000, quantity=1.0, leg_a_quantity=0.002, leg_b_quantity=0.03,
            gross_spread=2.0, total_cost=0.0005, net_profit=0.0031, annualized_return=0.1,
            entry_threshold=1.0, exit_target=0.2,
        )
        db.add(opportunity)
        db.commit()
        opportunity_id = opportunity.id

        synced = SimpleNamespace(
            time_diff_ms=0.0,
            leg_a=SimpleNamespace(local_recv_ts=utc_now(), bid=99.0, ask=100.0),
            leg_b=SimpleNamespace(local_recv_ts=utc_now(), bid=102.0, ask=103.0),
        )
        monkeypatch.setattr("app.execution.preflight.require_paper_execution_ready", lambda db: None)
        monkeypatch.setattr("app.execution.preflight.strict_sync_for_execution", lambda *args: (synced, "", False))
        monkeypatch.setattr("app.execution.coordinator.mt5_session_state", lambda mapping: SimpleNamespace())
        monkeypatch.setattr("app.execution.coordinator.mt5_action_allowed", lambda *args: (True, ""))
        monkeypatch.setattr("app.execution.coordinator.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

        result = create_open_intent(
            db,
            opportunity_id=opportunity_id,
            requested_by="test",
            idempotency_key="open:gold:1",
            source="manual",
        )
        group_id = result.intent.hedge_group_id
        db.commit()
        assert db.get(HedgeGroup, group_id).status == "opening"
        assert db.get(HedgeGroup, group_id).entry_spread == 0
        assert db.get(ArbitrageOpportunity, opportunity_id).status == "executing"
    calls = []

    run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FillingAdapter(venue, calls),
    )

    with factory() as db:
        group = db.get(HedgeGroup, group_id)
        assert group.status == "open"
        assert group.opened_at is not None
        assert group.fees == pytest.approx(0.02)
        assert group.trigger_spread == 2.0
        assert group.entry_spread == -1.0
        # 新执行模型的交易所回报本身足以还原真实价差，不依赖迁移期旧 Fill 表。
        db.query(Fill).delete()
        assert actual_entry_spread_from_fills(db, group) == -1.0
        assert db.get(ArbitrageOpportunity, opportunity_id).status == "executed"
        open_orders = db.query(Order).filter_by(hedge_group_id=group_id, reduce_only=False).all()
        assert len(open_orders) == 2


def test_open_request_rechecks_current_bbo_even_without_active_refresh(monkeypatch) -> None:
    factory = _factory()
    with factory() as db:
        db.add(StrategySetting(
            execution_mode="paper", paper_use_live_account_risk=False,
            min_net_profit=0.0, min_total_profit=0.0,
        ))
        db.add(SymbolMapping(
            symbol="GOLD", leg_a_venue="binance", leg_a_venue_symbol="XAUUSDT", leg_a_symbol="XAUUSDT",
            leg_b_venue="mt5", leg_b_symbol="XAUUSD", mt5_symbol="XAUUSD",
        ))
        opportunity = ArbitrageOpportunity(
            symbol="GOLD", direction="long_leg_a_short_leg_b", status="executable",
            notional=4000, quantity=1.0, leg_a_quantity=0.002, leg_b_quantity=0.03,
            gross_spread=2.0, total_cost=0.5, net_profit=1.5, annualized_return=0.1,
            entry_threshold=1.0, exit_target=0.2,
        )
        db.add(opportunity)
        db.commit()
        synced = SimpleNamespace(
            time_diff_ms=0.0,
            leg_a=SimpleNamespace(local_recv_ts=utc_now(), bid=100.0, ask=101.0),
            leg_b=SimpleNamespace(local_recv_ts=utc_now(), bid=101.5, ask=102.0),
        )
        monkeypatch.setattr("app.execution.preflight.require_paper_execution_ready", lambda db: None)
        # refreshed=False 表示缓存报价已经足够新，仍必须按该 BBO 复核入场条件。
        monkeypatch.setattr("app.execution.preflight.strict_sync_for_execution", lambda *args: (synced, "", False))
        monkeypatch.setattr("app.execution.coordinator.mt5_session_state", lambda mapping: SimpleNamespace())
        monkeypatch.setattr("app.execution.coordinator.mt5_action_allowed", lambda *args: (True, ""))

        with pytest.raises(ValueError, match="当前价差不再满足入场线"):
            create_open_intent(
                db, opportunity_id=opportunity.id, requested_by="test",
                idempotency_key="open:gold:stale-signal", source="manual",
            )

        assert db.query(ExecutionIntent).count() == 0


def test_maker_open_persists_only_maker_stage_and_hedge_template(monkeypatch) -> None:
    factory = _factory()
    with factory() as db:
        db.add(StrategySetting(
            execution_mode="paper", paper_use_live_account_risk=False,
            min_net_profit=0.0, min_total_profit=0.0,
        ))
        db.add(SymbolMapping(
            symbol="GOLD", leg_a_venue="binance", leg_a_venue_symbol="XAUUSDT", leg_a_symbol="XAUUSDT",
            leg_b_venue="mt5", leg_b_symbol="XAUUSD", mt5_symbol="XAUUSD",
            execution_style="maker_then_market", maker_leg="a", maker_offset_bps=1.0,
            maker_order_ttl_seconds=3, maker_unfilled_action="cancel",
        ))
        opportunity = ArbitrageOpportunity(
            symbol="GOLD", direction="long_leg_a_short_leg_b", status="executable",
            notional=4000, quantity=1.0, leg_a_quantity=0.002, leg_b_quantity=0.03,
            gross_spread=2.0, total_cost=0.0005, net_profit=0.0031, annualized_return=0.1,
            entry_threshold=1.0, exit_target=0.2,
        )
        db.add(opportunity)
        db.commit()
        synced = SimpleNamespace(
            time_diff_ms=0.0,
            leg_a=SimpleNamespace(local_recv_ts=utc_now(), bid=3998.0, ask=3998.5),
            leg_b=SimpleNamespace(local_recv_ts=utc_now(), bid=4000.0, ask=4000.5),
        )
        monkeypatch.setattr("app.execution.preflight.require_paper_execution_ready", lambda db: None)
        monkeypatch.setattr("app.execution.preflight.strict_sync_for_execution", lambda *args: (synced, "", False))
        monkeypatch.setattr("app.execution.coordinator.mt5_session_state", lambda mapping: SimpleNamespace())
        monkeypatch.setattr("app.execution.coordinator.mt5_action_allowed", lambda *args: (True, ""))
        monkeypatch.setattr("app.execution.coordinator.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

        result = create_open_intent(
            db, opportunity_id=opportunity.id, requested_by="test",
            idempotency_key="open:maker:1", source="manual",
        )
        db.commit()
        legs = db.query(ExecutionLeg).filter_by(intent_id=result.intent.id).all()
        payload = json.loads(db.query(ExecutionOutbox).filter_by(intent_id=result.intent.id).one().payload)

        assert result.intent.execution_style == "maker_then_market"
        assert len(legs) == 1
        assert legs[0].role == "MAKER"
        assert legs[0].post_only is True
        assert legs[0].order_type == "limit"
        assert legs[0].limit_price == synced.leg_a.bid
        assert payload["hedge_template"]["venue"] == "mt5"
        assert payload["hedge_template"]["role"] == "HEDGE"
        assert payload["maker_ttl_seconds"] == 3


def test_maker_close_uses_explicit_binance_position_side_without_reduce_only(monkeypatch) -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="paper")
        mapping = db.query(SymbolMapping).filter_by(symbol="GOLD").one()
        mapping.execution_style = "maker_then_market"
        mapping.maker_leg = "a"
        mapping.maker_order_ttl_seconds = 3
        mapping.maker_unfilled_action = "cancel"
        synced = SimpleNamespace(
            leg_a=SimpleNamespace(bid=4000.0, ask=4000.5),
            leg_b=SimpleNamespace(bid=3999.0, ask=3999.5),
        )
        monkeypatch.setattr("app.execution.preflight.strict_sync_for_execution", lambda *args: (synced, "", False))

        result = create_close_intent(
            db, group_id=group.id, reason="maker close", requested_by="test",
            idempotency_key="close:maker:1",
        )
        db.commit()
        maker = db.query(ExecutionLeg).filter_by(intent_id=result.intent.id).one()
        payload = json.loads(db.query(ExecutionOutbox).filter_by(intent_id=result.intent.id).one().payload)

        assert maker.role == "MAKER"
        assert maker.action == "CLOSE"
        assert maker.order_side == "SELL"
        assert maker.limit_price == synced.leg_a.ask
        assert maker.position_side == "LONG"
        assert maker.venue_reduce_only is False
        assert payload["hedge_template"]["order_side"] == "BUY"
        assert payload["hedge_template"]["position_side"] == "SHORT"


def test_recovery_intent_flattens_only_confirmed_group_residual() -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="live")
        group.status = "manual_intervention"
        _opening_fill(db, group, "binance", "buy", 0.001, 4000)
        old = ExecutionIntent(
            hedge_group_id=group.id, intent_type="OPEN", execution_mode="live",
            idempotency_key="failed-open", status="RECOVERY_REQUIRED",
        )
        db.add(old)
        db.commit()
        group_id = group.id
        old_id = old.id

        result = create_recovery_intent(
            db, group_id=group_id, reason="flatten confirmed residual",
            requested_by="test", idempotency_key="recover:gold:1",
        )
        db.commit()
        legs = db.query(ExecutionLeg).filter_by(intent_id=result.intent.id).all()
        assert [(leg.venue, leg.action, leg.order_side, leg.venue_order_quantity) for leg in legs] == [
            ("binance", "CLOSE", "SELL", 0.001),
        ]
        assert db.get(ExecutionIntent, old_id).status == "SUPERSEDED"

    calls = []
    run_execution_outbox_once(
        session_factory=factory,
        adapter_factory=lambda venue, mode: FillingAdapter(venue, calls),
    )
    with factory() as db:
        assert db.get(HedgeGroup, group_id).status == "closed"
        assert db.query(ExecutionIntent).filter_by(intent_type="RECOVER", status="COMPLETED").count() == 1


def test_recovery_intent_refuses_unknown_pending_order() -> None:
    factory = _factory()
    with factory() as db:
        group = _group_and_mapping(db, mode="live")
        group.status = "manual_intervention"
        _opening_fill(db, group, "binance", "buy", 0.001, 4000)
        db.add(Order(
            hedge_group_id=group.id, platform="mt5", symbol="GOLD", side="sell",
            quantity=0.01, status="accepted", external_order_id="pending-mt5",
        ))
        db.commit()

        import pytest
        with pytest.raises(ValueError, match="未确认订单"):
            create_recovery_intent(
                db, group_id=group.id, reason="unsafe", requested_by="test",
                idempotency_key="recover:gold:pending",
            )


def test_execution_recheck_uses_exit_target_and_round_trip_fees() -> None:
    opportunity = SimpleNamespace(
        direction="long_leg_a_short_leg_b",
        entry_threshold=1.0,
        exit_target=1.5,
        leg_a_quantity=1.0,
        quantity=1.0,
        total_cost=0.6,
    )
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=99.0, ask=100.0),
        leg_b=SimpleNamespace(bid=102.0, ask=103.0),
    )
    strategy = SimpleNamespace(min_total_profit=0.0, min_net_profit=0.0)

    allowed, reason = refreshed_opportunity_still_executable(opportunity, synced, strategy)

    assert allowed is False
    assert "净利润不足" in reason
