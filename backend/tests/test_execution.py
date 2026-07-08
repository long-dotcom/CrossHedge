"""执行引擎测试：开仓/平仓、自动平仓、执行就绪检查、对冲池、Gateway、持仓成本。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Fill, HedgeGroup, HedgeGroupEvent, Order, Position,
    StrategySetting, SymbolMapping, SystemSetting, User,
)
from app.execution.engine import (
    _effective_close_exit_target, _execution_adapters, _final_close_still_executable,
    _has_position_effect, _is_pending_result, _maker_price,
    close_hedge_group, open_hedge_group,
)
from app.execution.gateway import (
    AdapterExecutionGateway, FillEvent, GatewayOrderResult,
    LegOrderIntent, OrderEvent, build_execution_gateway,
)
from app.execution.auto_closer import evaluate_auto_close, run_auto_close
from app.execution.auto_executor import _eligible, run_auto_execute
from app.execution.carry_costs import _mt5_swap_cost, _paper_hyperliquid_funding_cost
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.persistence import persist_hedge_pool_events
from app.execution.readiness import live_execution_readiness, paper_execution_readiness
from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.market.quotes import quote_cache
from app.market.orderbook import order_book_cache
from app.market.mt5_sessions import MT5SessionState, mt5_session_state
from conftest import _live_close_test_db, _seed_auto_close_quotes


def test_manual_intervention_close_marks_closed_when_no_leg_has_position(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", paper_leg_a_latency_ms_min=0, paper_leg_a_latency_ms_max=0, paper_leg_b_latency_ms_min=0, paper_leg_b_latency_ms_max=0))
    db.add(SymbolMapping(symbol="EUR", leg_a_venue_symbol="xyz:EUR", mt5_symbol="EURUSDz"))
    group = HedgeGroup(
        symbol="EUR",
        direction="long_leg_b_short_leg_a",
        status="manual_intervention",
        execution_mode="paper",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=8.8,
        leg_b_quantity=0.02,
        unrealized_pnl=12.5,
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append((intent.platform, intent.side, intent.quantity, intent.reduce_only))
            message = "Hyperliquid reduce-only 探针没有可平仓真实仓位: xyz:EUR" if intent.platform == "hyperliquid" else "MT5 reduce-only 未找到可平仓持仓: EURUSDz sell 0.02"
            result = AdapterOrderResult(False, "", "failed", 0.0, 0.0, 0.0, message)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "failed", "", intent.quantity, 0.0, 0.0, 0.0, message)
            return GatewayOrderResult(False, event, (), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda *args, **kwargs: [])
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))

    closed = close_hedge_group(db, group.id, "resolve no residual position")

    assert closed.status == "closed"
    assert closed.unrealized_pnl == 0.0
    assert "无可平仓位" in closed.close_reason
    assert submitted == [("hyperliquid", "buy", 8.8, True), ("mt5", "sell", 0.02, True)]
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "closed").count() == 1

def test_live_close_hedge_group_places_reverse_orders(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    submitted = []

    class FakeGateway:
        def __init__(self, platform: str) -> None:
            self.platform = platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter.platform))
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    group = close_hedge_group(db, group_id, "manual close")

    assert group.status == "closed"
    assert group.close_reason == "manual close"
    orders = db.query(Order).filter(Order.hedge_group_id == group_id).order_by(Order.platform).all()
    assert [(order.platform, order.side, order.status) for order in orders] == [("hyperliquid", "sell", "filled"), ("mt5", "buy", "filled")]
    assert [order.reduce_only for order in orders] == [True, True]
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert db.query(Fill).count() == 2

def test_hyperliquid_paper_fee_uses_venue_symbol_effective_taker_rate() -> None:
    provider_calls = []

    def fee_provider(symbol: str):
        provider_calls.append(symbol)
        return SimpleNamespace(taker_fee_rate=0.00009, maker_fee_rate=0.00003)

    adapter = PaperAdapter("hyperliquid", fee_rate_provider=fee_provider)
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "JPY-FEE", 100, 101, 10000, "test")

    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="JPY-FEE", venue_symbol="xyz:JPY", side="buy", quantity=10))

    assert result.success
    assert provider_calls == ["xyz:JPY"]
    assert result.order_event.fee == pytest.approx(10 * 101 * 0.00009)

def test_auto_close_uses_saved_exit_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=0.0)
        db.add(strategy)
        db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225"))
        group = HedgeGroup(
            symbol="JP225",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=500,
            quantity=1,
            leg_b_quantity=1,
            leg_a_quantity=1,
            open_cost=10,
            entry_spread=250,
            exit_target=170,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "JP225", 71330, 71331, 10000, "test")
        quote_cache.put("mt5", "JP225", 71490, 71495, 10000, "test")
        evaluation = evaluate_auto_close(db, strategy, group)
        assert evaluation.should_close
        assert evaluation.close_spread == 165
        assert evaluation.estimated_profit == 85

def test_mt5_swap_cost_uses_position_swap_sign(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_b_quantity=0.5,
        leg_a_quantity=1,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
    )
    db.add(group)
    db.commit()
    mapping = SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL")

    class FakeMT5:
        POSITION_TYPE_SELL = 1
        POSITION_TYPE_BUY = 0

        @staticmethod
        def positions_get(symbol=None):
            return [SimpleNamespace(symbol="USOIL", type=1, volume=0.5, swap=-1.25)]

    monkeypatch.setattr("app.execution.carry_costs._initialize_mt5", lambda mt5, settings: True)
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5)

    assert _mt5_swap_cost(db, group, mapping) == 1.25

def test_mt5_paper_fee_is_zero() -> None:
    adapter = PaperAdapter("mt5")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("mt5", "JPY-MT5-FEE", 100, 101, 10000, "test")

    result = gateway.submit_order(LegOrderIntent(platform="mt5", symbol="JPY-MT5-FEE", side="buy", quantity=10))

    assert result.success
    assert result.order_event.fee == 0.0

def test_open_blocks_when_mt5_session_disallows_open_before_hyperliquid_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_leg_b_short_leg_a",
        status="executable",
        notional=500,
        quantity=34.0,
        leg_a_quantity=34.0,
        leg_b_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    gateway_calls = []
    sync_calls = []

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr(
        "app.execution.engine.mt5_session_state",
        lambda mapping: MT5SessionState(mapping.symbol, "reduce_only", "MT5 当前只允许平仓", True, False, False, True, True),
    )
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: gateway_calls.append(adapter.platform))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_calls.append(args) or (None, "should not sync"))

    with pytest.raises(ValueError, match="MT5 当前不允许该方向新开仓"):
        open_hedge_group(db, opportunity.id)

    db.refresh(opportunity)
    assert "MT5 当前不允许该方向新开仓" in opportunity.reject_reason
    assert db.query(HedgeGroup).count() == 0
    assert db.query(Order).count() == 0
    assert db.query(Fill).count() == 0
    assert gateway_calls == []
    assert sync_calls == []

def test_live_execution_readiness_blocks_hyperliquid_live_submit_after_sdk_removal(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(
        SymbolMapping(
            symbol="OIL",
            leg_a_venue_symbol="OIL",
            mt5_symbol="USOIL",
            mt5_volume_step=0.01,
            mt5_contract_size=100,
            single_leg_action="manual_intervention",
            enabled=True,
        )
    )
    db.commit()

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    def fake_import(name):
        return FakeMT5() if name == "MetaTrader5" else object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    assert result["ready"] is False
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "hyperliquid_live_order_submit" in blocked

def test_hedge_pool_loads_and_cas_groups() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        group = HedgeGroup(
            symbol="POOL",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            leg_b_quantity=1,
            leg_a_quantity=1,
            entry_spread=20,
            exit_target=2,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()

        assert hedge_pool.load_from_db(db) == 1
        first = hedge_pool.try_mark_closing(group.id, "close", 10)
        second = hedge_pool.try_mark_closing(group.id, "close again", 10)

        assert first is not None
        assert first.status == "closing"
        assert second is None
        closed = hedge_pool.mark_closed(group.id, realized_pnl=9, fees_delta=0.1, reason="done")
        assert closed is not None
        assert hedge_pool.get(group.id) is None

def test_paper_hyperliquid_funding_cost_uses_actual_rates(monkeypatch) -> None:
    group = HedgeGroup(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_b_quantity=1,
        leg_a_quantity=1,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
    )
    mapping = SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225")

    monkeypatch.setattr(
        "app.execution.carry_costs._post_hyperliquid_info",
        lambda payload: [{"time": payload["startTime"] + 1, "fundingRate": "0.0001"}, {"time": payload["startTime"] + 2, "fundingRate": "-0.00005"}],
    )

    assert round(_paper_hyperliquid_funding_cost(group, mapping), 8) == 0.05
    group.direction = "long_leg_b_short_leg_a"
    assert round(_paper_hyperliquid_funding_cost(group, mapping), 8) == -0.05

def test_live_execution_readiness_blocks_residual_closed_group_position(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_leg_a_short_leg_b",
            status="closed",
            execution_mode="live",
            notional=1000,
            quantity=1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=1, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "live_residual_positions" in blocked

def test_live_execution_readiness_blocks_missing_live_prerequisites(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", enabled=True))
    db.commit()

    def fake_import(name):
        raise ImportError(name)

    settings = SimpleNamespace(
        hyperliquid_account_address="",
        mt5_live_order_enabled=False,
        mt5_login="",
        mt5_server="",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert {"global_live_switch", "hyperliquid_account_address", "hyperliquid_live_order_submit", "metatrader5_import"} <= blocked

def test_gateway_factory_uses_adapter_gateway_for_simulated_hyperliquid() -> None:
    adapter = PaperAdapter("hyperliquid")
    setattr(adapter, "simulated", True)

    built = build_execution_gateway(adapter)

    assert isinstance(built, AdapterExecutionGateway)

def test_paper_execution_readiness_blocks_real_mt5_account(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    settings = SimpleNamespace(
        mt5_demo_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
    )

    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker-demo", trade_mode=2)

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    def fake_import(name):
        if name == "MetaTrader5":
            return FakeMT5()
        return object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = paper_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "mt5_demo_account" in blocked

def test_live_execution_readiness_blocks_unmanaged_live_positions(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert "live_orphan_positions" in blocked

def test_run_auto_close_uses_pool_without_hedge_group_query(monkeypatch) -> None:
    hedge_pool._groups.clear()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    group = HedgeGroupSnapshot(
        id=999,
        symbol="POOL-AUTO",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_b_quantity=1,
        leg_a_quantity=1,
        open_cost=0,
        fees=0,
        funding=0,
        swap=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trigger_spread=20,
        entry_spread=20,
        entry_threshold=10,
        exit_target=2,
        overheat_threshold=0,
        close_reason="",
        opened_at=now,
        closed_at=None,
        source="test",
    )
    hedge_pool.upsert_group(group)
    quote_cache.put("hyperliquid", "POOL-AUTO", bid=100, ask=101, depth_notional=10000, source="test")
    quote_cache.put("mt5", "POOL-AUTO", bid=100, ask=101, depth_notional=10000, source="test")
    strategy = SimpleNamespace(
        auto_close_enabled=True,
        auto_close_live_enabled=False,
        auto_close_min_profit=0,
        max_holding_minutes=240,
        paper_leg_a_latency_ms_min=0,
        paper_leg_a_latency_ms_max=0,
        paper_leg_b_latency_ms_min=0,
        paper_leg_b_latency_ms_max=0,
    )
    mapping = SimpleNamespace(
        symbol="POOL-AUTO",
        leg_a_venue="hyperliquid",
        leg_b_venue="mt5",
        leg_a_venue_symbol="POOL-AUTO",
        mt5_symbol="POOL-AUTO",
        max_close_spread=2,
        allow_hold_through_mt5_close=True,
        execution_style="taker_taker",
        hl_close_order_type="market",
        mt5_close_order_type="market",
    )
    submitted = []

    class FakeDb:
        def query(self, model, *args, **kwargs):
            if model is HedgeGroup:
                raise AssertionError("auto close hot path must not query HedgeGroup")

            class EmptyQuery:
                def filter(self, *args, **kwargs):
                    return self

                def first(self):
                    return None

            return EmptyQuery()

        def add(self, item):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-pool", "filled", intent.quantity, 100.0, 0.0)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.0)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.0, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.auto_closer.get_strategy_setting", lambda db: strategy)
    monkeypatch.setattr("app.execution.auto_closer.enabled_mappings", lambda db: [mapping])
    monkeypatch.setattr("app.execution.auto_closer.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.auto_closer.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.auto_closer.prune_table_by_id", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.execution.auto_closer.breaker_is_blocked", lambda symbol: (False, 0.0, 0.0))
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=100, ask=101, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        leg_b=SimpleNamespace(bid=100, ask=101, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        time_diff_ms=0,
    )
    monkeypatch.setattr("app.execution.auto_closer.quote_synchronizer.synchronized", lambda *args, **kwargs: (synced, ""))

    closed = run_auto_close(FakeDb())

    assert closed == 1
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert hedge_pool.get(group.id) is None

def test_close_adopted_single_leg_group_only_closes_existing_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", allow_hold_through_mt5_close=True))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="manual_intervention",
        execution_mode="live",
        notional=1000,
        quantity=0.2,
        leg_a_quantity=0.0,
        leg_b_quantity=0.2,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def __init__(self, platform: str) -> None:
            self.platform = platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append((intent.platform, intent.side, intent.quantity))
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter.platform))
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})

    closed = close_hedge_group(db, group.id, "close adopted")

    assert closed.status == "closed"
    assert submitted == [("mt5", "buy", 0.2)]
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 1

def test_paper_open_records_actual_entry_spread_from_fills(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            price = 101.0 if intent.platform == "hyperliquid" else 103.0
            result = AdapterOrderResult(True, f"{intent.platform}-paper", "filled", intent.quantity, price, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, price, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, price, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda *args, **kwargs: ["hyperliquid"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert group.trigger_spread == 10
    assert group.entry_spread == 2.0
    assert group.fees == 0.2

def test_execution_maintenance_job_does_not_run_carry_cost(monkeypatch) -> None:
    from app.workers import scheduler as scheduler_module

    calls = []

    class FakeDb:
        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(scheduler_module, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(scheduler_module, "run_auto_execute", lambda db: calls.append("auto_execute"))
    monkeypatch.setattr(scheduler_module, "run_auto_close", lambda db: calls.append("auto_close"))
    monkeypatch.setattr(scheduler_module, "run_execution_reconcile", lambda db: calls.append("reconcile"))
    monkeypatch.setattr(scheduler_module, "run_carry_cost_sync", lambda db: calls.append("carry_cost"))
    scheduler_module._running = False
    scheduler_module._execution_running = False

    scheduler_module.execution_maintenance_job()

    assert calls == ["auto_execute", "auto_close", "reconcile"]

def test_auto_close_zero_axis_still_requires_min_profit() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=100.0, statistical_min_samples=200)
        db.add(strategy)
        group = HedgeGroup(
            symbol="OIL-ZERO-MIN",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=5000,
            quantity=0.07,
            leg_b_quantity=0.07,
            leg_a_quantity=70,
            entry_spread=0.847,
            exit_target=0.0,
            fees=0.45,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "OIL-ZERO-MIN", 72.69, 72.70, 10000, "test")
        quote_cache.put("mt5", "OIL-ZERO-MIN", 72.55, 72.55, 10000, "test")

        evaluation = evaluate_auto_close(db, strategy, group)

        assert not evaluation.should_close
        assert "利润不足" in evaluation.reason

def test_hyper_maker_price_is_normalized_to_tick_and_precision() -> None:
    mapping = SymbolMapping(symbol="EUR", leg_a_venue_symbol="xyz:EUR", mt5_symbol="EURUSD", price_precision=5, min_tick=0.00001)

    sell_price = _maker_price("sell", bid=1.1459, ask=1.1459, offset_bps=1.0, mapping=mapping)
    buy_price = _maker_price("buy", bid=1.1459, ask=1.1460, offset_bps=1.0, mapping=mapping)

    assert sell_price == 1.14602
    assert buy_price == 1.14578
    assert len(str(sell_price).split(".")[1]) <= 5

def test_paper_execution_readiness_allows_demo_account(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    settings = SimpleNamespace(
        mt5_demo_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker-demo",
    )

    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker-demo", trade_mode=self.ACCOUNT_TRADE_MODE_DEMO)

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    def fake_import(name):
        if name == "MetaTrader5":
            return FakeMT5()
        return object()

    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)

    result = paper_execution_readiness(db, settings)

    assert result["status"] == "ready"
    assert result["ready"] is True

def test_open_rejects_when_refreshed_quotes_no_longer_meet_entry(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=450,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(ask=100.0, bid=99.0, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        leg_b=SimpleNamespace(ask=106.0, bid=105.0),
        time_diff_ms=10,
    )
    sync_results = [(None, "行情未对齐，时间差 900ms"), (synced, "")]

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda mapping: ["hyperliquid", "mt5"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_results.pop(0))

    with pytest.raises(ValueError, match="主动刷新后价差不再满足入场线"):
        open_hedge_group(db, opportunity.id)

def test_hedge_pool_load_preserves_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        group = HedgeGroup(
            symbol="POOL-PRESERVE",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            leg_b_quantity=1,
            leg_a_quantity=1,
            entry_spread=20,
            unrealized_pnl=0,
        )
        db.add(group)
        db.commit()
        hedge_pool.load_from_db(db)
        snapshot = hedge_pool.get(group.id)
        assert snapshot is not None
        hedge_pool.upsert_group(snapshot.with_updates(unrealized_pnl=12.5))

        assert hedge_pool.load_from_db(db) == 1

        reloaded = hedge_pool.get(group.id)
        assert reloaded is not None
        assert reloaded.unrealized_pnl == 12.5

def test_open_quarantines_mt5_side_after_order_send_10044(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_leg_b_short_leg_a",
        status="executable",
        notional=500,
        quantity=34.0,
        leg_a_quantity=34.0,
        leg_b_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            if intent.platform == "hyperliquid":
                result = AdapterOrderResult(True, "hl-1", "filled", intent.quantity, 151.0, 0.1)
                event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 151.0, 0.1)
                fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 151.0, 0.1, result.external_order_id)
                return GatewayOrderResult(True, event, (fill,), result)
            result = AdapterOrderResult(False, "mt5-1", "rejected", 0.0, 0.0, 0.0, "MT5 order_send 失败 retcode=10044: mt5-hedge")
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "rejected", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0, result.error_message)
            return GatewayOrderResult(False, event, (), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="Done"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "manual_intervention"
    block = db.get(SystemSetting, "mt5_tradability_block:SPCX:buy")
    assert block is not None
    assert "retcode=10044" in block.value
    assert db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5", Order.status == "rejected").count() == 1

def test_auto_close_fallback_uses_symbol_max_close_spread_without_samples(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(statistical_min_samples=20, auto_close_min_profit=0)
        mapping = SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225", max_close_spread=10)
        group = HedgeGroup(
            symbol="JP225",
            direction="long_leg_a_short_leg_b",
            notional=1000,
            quantity=1,
            leg_a_quantity=1,
            entry_spread=100,
            entry_threshold=100,
            exit_target=0,
            open_cost=0,
            opened_at=now,
            status="open",
            unrealized_pnl=20,
        )
        db.add_all([strategy, mapping, group])
        db.commit()
        synced = SimpleNamespace(
            leg_a=SimpleNamespace(bid=110, ask=111),
            leg_b=SimpleNamespace(bid=100, ask=101),
        )
        monkeypatch.setattr("app.execution.auto_closer.quote_synchronizer.synchronized", lambda *args, **kwargs: (synced, ""))

        evaluation = evaluate_auto_close(db, strategy, group)

    assert evaluation.exit_target == 10
    assert evaluation.should_close is True

def test_hyperliquid_paper_market_order_uses_l2_average_price() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "L2-PAPER", 100, 101, 10000, "test")
    order_book_cache.put(
        "hyperliquid",
        "L2-PAPER",
        bids=[(100.0, 10.0)],
        asks=[(101.0, 1.0), (103.0, 1.0)],
        source="test",
    )

    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="L2-PAPER", side="buy", quantity=2))

    assert result.success
    assert result.order_event.average_price == 102.0
    assert result.fill_events[0].price == 102.0

def test_paper_open_uses_hyperliquid_sim_and_mt5_demo_adapters(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    seen = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            seen.append((adapter.platform, getattr(adapter, "simulated", False), getattr(adapter, "demo", False), getattr(adapter, "live", False)))

        def submit_order(self, intent, *, paper_latency_ms=0):
            result = AdapterOrderResult(True, f"{intent.platform}-paper", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert ("hyperliquid", True, False, False) in seen
    assert ("mt5", True, True, False) in seen

def test_auto_close_final_check_uses_current_symbol_max_close_spread(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_min_profit=0)
        mapping = SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCX", max_close_spread=-0.11)
        group = HedgeGroup(
            symbol="SPCX",
            direction="long_leg_b_short_leg_a",
            status="open",
            execution_mode="paper",
            notional=2000,
            quantity=0.13,
            leg_b_quantity=0.13,
            leg_a_quantity=13,
            entry_spread=0.14,
            exit_target=0.047,
            fees=0.18,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add_all([strategy, mapping, group])
        db.commit()
        synced = SimpleNamespace(
            leg_a=SimpleNamespace(bid=157.58, ask=157.59, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
            leg_b=SimpleNamespace(bid=157.59, ask=157.65, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
            time_diff_ms=0,
        )
        monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (synced, ""))

        ok, reason = _final_close_still_executable(db, group, mapping, strategy, "平仓价差回归至退出线: 0.00 <= 0.05")

    assert _effective_close_exit_target(group, mapping) == pytest.approx(-0.11)
    assert ok is False
    assert "平仓价差 0.000000 > 退出线 -0.110000" in reason

def test_live_execution_readiness_blocks_failed_read_probes(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return None

        def last_error(self):
            return (500, "no account")

        def shutdown(self):
            return True

    def fake_import(name):
        return FakeMT5() if name == "MetaTrader5" else object()

    def failing_urlopen(req, timeout):
        raise TimeoutError("timeout")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", fake_import)
    monkeypatch.setattr("app.execution.readiness.request.urlopen", failing_urlopen)

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    blocked = {item["component"] for item in result["checks"] if item["status"] == "block"}
    assert {"hyperliquid_read_probe", "mt5_read_probe"} <= blocked

def test_paper_open_waits_for_hyperliquid_fill_before_mt5(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=450,
        quantity=1.0,
        leg_a_quantity=0.00625,
        leg_b_quantity=1.0,
        gross_spread=10,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    submitted = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-pending", "accepted", 0.0, 0.0, 0.0, "timeout")
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "accepted", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0)
            return GatewayOrderResult(True, event, (), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "opening"
    assert opportunity.status == "executing"
    assert [intent.platform for intent in submitted] == ["hyperliquid"]
    orders = db.query(Order).filter(Order.hedge_group_id == group.id).all()
    assert [(order.platform, order.status) for order in orders] == [("hyperliquid", "accepted")]
    assert db.query(Fill).count() == 0

def test_auto_close_allows_zero_axis_close_without_exit_target() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        strategy = StrategySetting(auto_close_enabled=True, auto_close_min_profit=0.0, statistical_min_samples=200)
        db.add(strategy)
        group = HedgeGroup(
            symbol="OIL-ZERO",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=5000,
            quantity=0.07,
            leg_b_quantity=0.07,
            leg_a_quantity=70,
            entry_spread=0.847,
            exit_target=0.0,
            fees=0.45,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(group)
        db.commit()
        quote_cache.put("hyperliquid", "OIL-ZERO", 72.69, 72.70, 10000, "test")
        quote_cache.put("mt5", "OIL-ZERO", 72.55, 72.55, 10000, "test")

        evaluation = evaluate_auto_close(db, strategy, group)

        assert evaluation.should_close
        assert evaluation.close_spread == pytest.approx(-0.14)
        assert "零轴" in evaluation.reason

def test_live_open_blocks_when_readiness_has_blockers(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        gross_spread=10,
        trigger_leg_a_bid=99.0,
        trigger_leg_a_ask=101.0,
        trigger_leg_b_bid=110.0,
        trigger_leg_b_ask=111.0,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    monkeypatch.setattr(
        "app.execution.engine.live_execution_readiness",
        lambda db: {"checks": [{"component": "hyperliquid_live_order_submit", "status": "block", "message": "Hyperliquid 实盘下单未启用"}]},
    )

    with pytest.raises(ValueError, match="实盘执行就绪检查未通过"):
        open_hedge_group(db, opportunity.id)

def test_live_open_orders_are_not_reduce_only(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="live"))
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    opportunity = ArbitrageOpportunity(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        gross_spread=10,
        trigger_leg_a_bid=99.0,
        trigger_leg_a_ask=101.0,
        trigger_leg_b_bid=110.0,
        trigger_leg_b_ask=111.0,
        unit_cost=1,
        unit_net_profit=9,
        entry_threshold=8,
        exit_target=2,
        total_cost=1,
        net_profit=9,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    quote_cache.put("hyperliquid", "OIL", 100, 101, 10000, "test")
    quote_cache.put("mt5", "OIL", 100, 101, 10000, "test")
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert group.trigger_leg_a_bid == 99.0
    assert group.trigger_leg_a_ask == 101.0
    assert group.trigger_leg_b_bid == 110.0
    assert group.trigger_leg_b_ask == 111.0
    assert [intent.reduce_only for intent in submitted] == [False, False]
    assert {order.reduce_only for order in db.query(Order).filter(Order.hedge_group_id == group.id).all()} == {False}

def test_paper_close_realized_pnl_uses_actual_close_fills(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        entry_spread=5.0,
        open_cost=999.0,
        fees=0.2,
        unrealized_pnl=999.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            price = 101.0 if intent.platform == "hyperliquid" else 103.0
            result = AdapterOrderResult(True, f"{intent.platform}-close", "filled", intent.quantity, price, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, price, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, price, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda *args, **kwargs: ["hyperliquid"])
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))

    closed = close_hedge_group(db, group.id, "manual close")

    assert closed.status == "closed"
    assert closed.unrealized_pnl == 0.0
    assert round(closed.realized_pnl, 6) == 2.6

def test_live_close_hedge_group_keeps_pending_orders_closing(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-accepted", "accepted", 0.0, 0.0, 0.0)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "accepted", result.external_order_id, intent.quantity, 0.0, 0.0, 0.0)
            return GatewayOrderResult(True, event, (), result)

    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.live_execution_readiness", lambda db: {"checks": []})
    group = close_hedge_group(db, group_id, "manual close")

    assert group.status == "closing"
    assert "待成交" in group.close_reason
    assert db.query(Order).filter(Order.hedge_group_id == group_id, Order.status == "accepted").count() == 1
    assert [intent.platform for intent in submitted] == ["hyperliquid"]
    assert db.query(Fill).count() == 0

def test_paper_live_parallel_submits_hyperliquid_and_mt5_without_waiting(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", paper_leg_a_latency_ms_min=0, paper_leg_a_latency_ms_max=0, paper_leg_b_latency_ms_min=0, paper_leg_b_latency_ms_max=0))
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=450,
        quantity=1.0,
        leg_a_quantity=0.00015,
        leg_b_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    submitted = []

    class FakeGateway:
        def __init__(self, adapter) -> None:
            self.platform = adapter.platform

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway(adapter))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: (SimpleNamespace(leg_a=SimpleNamespace(local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None), bid=99.0, ask=101.0), leg_b=SimpleNamespace(bid=110.0, ask=111.0), time_diff_ms=0), ""))
    monkeypatch.setattr("app.execution.engine.pre_trade_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, reason=""))
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=True, paper_live_parallel_execution=True, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert group.status == "open"
    assert {intent.platform for intent in submitted} == {"hyperliquid", "mt5"}
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2

def test_live_execution_readiness_requires_managed_position_side_and_quantity(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="live_trading_enabled", value="true"))
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1,
            leg_a_quantity=1.0,
            leg_b_quantity=0.1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=70, mark_price=71))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="short", quantity=1.0, entry_price=70, mark_price=71))
    db.commit()

    class FakeMT5:
        def initialize(self, **kwargs):
            return True

        def account_info(self):
            return SimpleNamespace(login=123, server="broker")

        def last_error(self):
            return (0, "")

        def shutdown(self):
            return True

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"marginSummary": {}, "assetPositions": []}).encode("utf-8")

    settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
        mt5_live_order_enabled=True,
        mt5_login="123",
        mt5_password="pwd",
        mt5_server="broker",
    )
    monkeypatch.setattr("app.execution.readiness.import_module", lambda name: FakeMT5() if name == "MetaTrader5" else object())
    monkeypatch.setattr("app.execution.readiness.request.urlopen", lambda req, timeout: FakeResponse())

    result = live_execution_readiness(db, settings)

    assert result["status"] == "blocked"
    orphan = next(item for item in result["checks"] if item["component"] == "live_orphan_positions")
    assert "mt5:USOIL:short:0.2" in orphan["message"]
    assert "hyperliquid:OIL:short:1.0" in orphan["message"]

def test_auto_execute_waits_for_mt5_tradability_cache(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", auto_execute_enabled=True, auto_execute_min_hold_ms=0, auto_execute_confirm_ticks=1))
    db.add(
        ArbitrageOpportunity(
            symbol="SPCX",
            direction="long_leg_b_short_leg_a",
            status="executable",
            notional=500,
            quantity=34.0,
            leg_a_quantity=34.0,
            leg_b_quantity=0.34,
            gross_spread=0.2,
            unit_cost=0.01,
            unit_net_profit=0.19,
            entry_threshold=0.1,
            exit_target=0.0,
            total_cost=0.34,
            net_profit=6.46,
            annualized_return=0.1,
        )
    )
    db.commit()
    calls = []

    monkeypatch.setattr("app.execution.auto_executor.mt5_tradability_cache.initialized", lambda: False)
    monkeypatch.setattr("app.execution.auto_executor.open_hedge_group", lambda *args, **kwargs: calls.append(args))

    assert run_auto_execute(db) == 0
    assert calls == []
    assert "缓存尚未初始化" in db.query(SystemLog).order_by(SystemLog.id.desc()).first().message

def test_auto_close_skips_live_group_without_live_switch(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=False)
    _seed_auto_close_quotes()
    called = []
    hedge_pool.load_from_db(db)

    closed = run_auto_close(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 0
    assert called == []
    assert group.status == "open"

def test_gateway_factory_uses_adapter_gateway_for_live_hyperliquid() -> None:
    built = build_execution_gateway(HyperliquidAdapter(live=True))
    assert isinstance(built, AdapterExecutionGateway)
    non_live = build_execution_gateway(PaperAdapter("hyperliquid"))
    assert isinstance(non_live, AdapterExecutionGateway)
    fallback = build_execution_gateway(PaperAdapter("mt5"))
    assert isinstance(fallback, AdapterExecutionGateway)

def test_open_refreshes_execution_quotes_after_strict_sync_failure(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper", paper_leg_a_latency_ms_min=0, paper_leg_a_latency_ms_max=0, paper_leg_b_latency_ms_min=0, paper_leg_b_latency_ms_max=0))
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225z"))
    opportunity = ArbitrageOpportunity(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="executable",
        notional=450,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=1.0,
        gross_spread=20,
        unit_cost=1,
        unit_net_profit=19,
        entry_threshold=10,
        exit_target=2,
        total_cost=1,
        net_profit=19,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(ask=100.0, bid=99.0, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        leg_b=SimpleNamespace(ask=121.0, bid=120.0),
        time_diff_ms=300,
    )
    sync_results = [(None, "行情过期，最大延迟 3000ms"), (synced, "")]
    refreshed = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            result = AdapterOrderResult(True, f"{intent.platform}-open", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.refresh_execution_quotes", lambda mapping, **kwargs: refreshed.append((mapping.symbol, kwargs.get("refresh_mt5"))) or ["hyperliquid", "mt5"])
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.engine.mt5_market_order_check", lambda *args, **kwargs: SimpleNamespace(allowed=True, message="ok"))
    monkeypatch.setattr("app.execution.engine.quote_synchronizer.synchronized", lambda *args, **kwargs: sync_results.pop(0))
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=False, paper_live_parallel_execution=False, strict_quote_sync_ms=500, quote_stale_ms=1500, default_slippage_bps=0))

    group = open_hedge_group(db, opportunity.id)

    assert refreshed == [("JP225", None), ("JP225", False)]
    assert group.status == "open"
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2

def test_execution_gateway_preserves_adapter_rejection() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    result = gateway.submit_order(
        LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=2, order_type="limit", price=102, post_only=True)
    )
    assert not result.success
    assert result.order_event.status == "rejected"
    assert "post-only" in result.order_event.message
    assert result.fill_events == ()

def test_live_close_blocks_when_readiness_has_blockers(monkeypatch) -> None:
    db, group_id = _live_close_test_db()
    monkeypatch.setattr(
        "app.execution.engine.live_execution_readiness",
        lambda db: {"checks": [{"component": "mt5_live_order_enabled", "status": "block", "message": "MT5_LIVE_ORDER_ENABLED 未开启"}]},
    )

    with pytest.raises(ValueError, match="实盘执行就绪检查未通过"):
        close_hedge_group(db, group_id, "manual close")

def test_auto_close_paper_group_submits_reverse_orders(monkeypatch) -> None:
    db, group_id = _live_close_test_db(auto_close_live_enabled=True, live_trading_enabled=True)
    db.get(StrategySetting, 1).execution_mode = "paper"
    group_row = db.get(HedgeGroup, group_id)
    group_row.execution_mode = "paper"
    db.commit()
    hedge_pool.load_from_db(db)
    _seed_auto_close_quotes()
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, f"{intent.platform}-auto-close", "filled", intent.quantity, 100.0, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.0, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.0, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.auto_closer.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr("app.execution.auto_closer.build_execution_gateway", lambda adapter: FakeGateway())

    closed = run_auto_close(db)
    persist_hedge_pool_events(db)
    group = db.get(HedgeGroup, group_id)

    assert closed == 1
    assert group.status == "closed"
    assert "平仓价差回归" in group.close_reason
    assert {order.reduce_only for order in db.query(Order).filter(Order.hedge_group_id == group.id).all()} == {True}
    assert [intent.reduce_only for intent in submitted] == [True, True]
    assert db.query(Fill).count() == 2

def test_accepted_order_without_fill_is_pending_not_position_effect() -> None:
    accepted = AdapterOrderResult(True, "nt-live-1", "accepted", 0.0, 0.0, 0.0)
    filled = AdapterOrderResult(True, "nt-live-2", "filled", 0.01, 65000.0, 0.1)
    assert _is_pending_result(accepted)
    assert not _has_position_effect(accepted)
    assert _has_position_effect(filled)

def test_execution_gateway_maps_adapter_fill_event() -> None:
    adapter = PaperAdapter("hyperliquid")
    gateway = AdapterExecutionGateway(adapter)
    quote_cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    result = gateway.submit_order(LegOrderIntent(platform="hyperliquid", symbol="BTC", side="buy", quantity=2))
    assert result.success
    assert result.order_event.status == "filled"
    assert result.order_event.filled_quantity == 2
    assert len(result.fill_events) == 1
    assert result.fill_events[0].price == 101

def test_open_blocks_when_mt5_order_check_rejects_before_hyperliquid_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(execution_mode="paper"))
    db.add(SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCXz"))
    opportunity = ArbitrageOpportunity(
        symbol="SPCX",
        direction="long_leg_b_short_leg_a",
        status="executable",
        notional=500,
        quantity=34.0,
        leg_a_quantity=34.0,
        leg_b_quantity=0.34,
        gross_spread=0.2,
        unit_cost=0.01,
        unit_net_profit=0.19,
        entry_threshold=0.1,
        exit_target=0.0,
        total_cost=0.34,
        net_profit=6.46,
        annualized_return=0.1,
    )
    db.add(opportunity)
    db.commit()
    gateway_calls = []
    checks = []

    monkeypatch.setattr("app.execution.engine.paper_execution_readiness", lambda db: {"checks": []})
    monkeypatch.setattr("app.execution.engine.mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr(
        "app.execution.engine.mt5_market_order_check",
        lambda symbol, side, quantity, **kwargs: checks.append((symbol, side, quantity, kwargs)) or SimpleNamespace(allowed=False, message="retcode=10044: Only position closing is allowed"),
    )
    monkeypatch.setattr("app.execution.engine.build_execution_gateway", lambda adapter: gateway_calls.append(adapter.platform))

    with pytest.raises(ValueError, match="MT5 当前订单预检查失败"):
        open_hedge_group(db, opportunity.id)

    db.refresh(opportunity)
    assert checks == [("SPCXz", "buy", 0.34, {"demo": True})]
    assert "retcode=10044" in opportunity.reject_reason
    assert db.query(HedgeGroup).count() == 0
    assert db.query(Order).count() == 0
    assert gateway_calls == []
