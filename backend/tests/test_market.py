"""扫描器、行情、报价同步、订单簿、MT5 会话、策略信号与成本估算测试。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    ArbitrageOpportunity, Base, SpreadCurrent, SpreadDirectionCurrent,
    RiskSetting, StrategySetting, SymbolMapping,
)
from app.market import scanner as scanner_module
from app.market import symbols as symbol_module
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.quotes import QuoteCache, QuoteSynchronizer, quote_cache
from app.market.scan_state import scan_state_store
from app.market.mt5_sessions import MT5SessionState, mt5_action_allowed, mt5_session_state
from app.market.mt5_schedule import apply_mt5_session_template, infer_template, local_schedule_state
from app.strategy.cost import estimate_cost
from app.strategy.live_costs import _estimate_mt5_swap_cost, _hyperliquid_effective_fee_rates
from app.strategy.signals import evaluate_signal
from app.strategy.spread_math import spreads_for_direction
from app.risk.engine import pre_trade_check
from app.api import markets as markets_api
from app.api import settings_api
from app.schemas import SymbolMappingIn
from app.workers.market_data import MarketDataManager, _exchange_time_from_hyperliquid_ms, hyperliquid_symbol_map, l2book_subscription


def test_quote_synchronizer_rejects_unsynced_quotes() -> None:
    cache = QuoteCache()
    sync = QuoteSynchronizer(cache)
    cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    quote = cache.put("mt5", "BTC", 102, 103, 10000, "test")
    object.__setattr__(quote, "local_recv_ts", quote.local_recv_ts.replace(year=quote.local_recv_ts.year - 1))
    synced, reason = sync.synchronized("BTC", "strict", max_time_diff_ms=100, max_age_ms=1000)
    assert synced is None
    assert "过期" in reason or "未对齐" in reason

def test_scanner_gate_combination_keeps_blockers_separate_from_signal() -> None:
    signal_gate = scanner_module.GateResult("executable", "signal ok", "signal")
    liquidity_gate = scanner_module.GateResult("candidate", "depth low", "liquidity", "liquidity")
    market_gate = scanner_module.GateResult("rejected", "mt5 blocked", "market", "market")

    assert scanner_module._combine_gates(signal_gate, liquidity_gate, market_gate) == market_gate
    assert scanner_module._combine_gates(signal_gate, liquidity_gate, scanner_module.GateResult("pass", "", "market")) == liquidity_gate
    assert scanner_module._combine_gates(signal_gate, scanner_module.GateResult("pass", "", "liquidity"), scanner_module.GateResult("pass", "", "market")).status == "executable"

def test_quote_synchronizer_accepts_aligned_quotes() -> None:
    cache = QuoteCache()
    sync = QuoteSynchronizer(cache)
    cache.put("hyperliquid", "BTC", 100, 101, 10000, "test")
    cache.put("mt5", "BTC", 102, 103, 10000, "test")
    synced, reason = sync.synchronized("BTC", "strict", max_time_diff_ms=500, max_age_ms=1000)
    assert synced is not None
    assert reason == ""

def test_hyperliquid_maker_open_taker_close_fee() -> None:
    cost = estimate_cost(
        notional=1000,
        leg_b_bid=100,
        leg_b_ask=100,
        max_slippage_bps=0,
        leg_a_fee_rate=0.00015,
        leg_a_close_fee_rate=0.00045,
        leg_a_funding_rate=0,
        leg_b_commission_rate=0,
        leg_b_swap_cost=0,
    )
    assert cost.leg_a_fee == 0.6

def test_live_market_data_starts_single_native_projection_thread(monkeypatch) -> None:
    manager = MarketDataManager()
    monkeypatch.setattr(manager, "_run", lambda: None)
    try:
        manager.start()
        assert manager._thread is not None
        assert manager._thread.name == "native-market-data"
    finally:
        manager.stop()

def test_symbol_spread_limits_tighten_statistical_thresholds() -> None:
    mapping = SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225", min_entry_spread=150, max_close_spread=12)

    assert scanner_module._effective_entry_threshold(mapping, 120) == 150
    assert scanner_module._effective_entry_threshold(mapping, 180) == 180
    assert scanner_module._effective_exit_target(mapping, 20) == 12
    assert scanner_module._effective_exit_target(mapping, 0) == 12

def test_delete_symbol_mapping_clears_current_scan_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    mapping = SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTC", mt5_symbol="BTCUSD", enabled=True)
    db.add(mapping)
    db.flush()
    db.add(SpreadCurrent(symbol="BTC", direction="none", leg_a_bid=1, leg_a_ask=1, leg_b_bid=1, leg_b_ask=1, quantity=1, gross_spread=0, unit_cost=0, unit_net_profit=0, total_cost=0, net_profit=0, annualized_return=0, status="rejected"))
    db.add(ArbitrageOpportunity(symbol="BTC", direction="long_leg_b_short_leg_a", notional=1, quantity=1, gross_spread=1, total_cost=0, net_profit=1, annualized_return=1, status="candidate"))
    db.commit()
    scan_state_store.update([{"symbol": "BTC"}], [{"symbol": "BTC", "status": "candidate"}])

    settings_api.delete_symbol_mapping(mapping.id, SimpleNamespace(id=1), db)

    state = scan_state_store.snapshot()
    assert state["spreads"] == []
    assert state["opportunities"] == []
    assert db.query(SpreadCurrent).filter(SpreadCurrent.symbol == "BTC").count() == 0
    assert db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.symbol == "BTC", ArbitrageOpportunity.status == "candidate").count() == 0

def test_mt5_session_template_infers_spcx_as_stock() -> None:
    mapping = SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCXz")
    assert infer_template(mapping) == "stock_us_close_only"

def test_mt5_positive_swap_reduces_cost() -> None:
    cost = _estimate_mt5_swap_cost(swap_value=10.0, swap_mode=1, point=0.01, contract_size=1.0, quantity=1, holding_days=1)
    assert cost == -0.1

def test_enabled_mappings_cache_requires_explicit_clear() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        symbol_module.clear_symbol_mapping_cache()
        db.add(SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTC", mt5_symbol="BTCUSD", enabled=True))
        db.commit()

        first = symbol_module.enabled_mappings(db)
        db.add(SymbolMapping(symbol="ETH", leg_a_venue_symbol="ETH", mt5_symbol="ETHUSD", enabled=True))
        db.commit()
        cached = symbol_module.enabled_mappings(db)
        symbol_module.clear_symbol_mapping_cache()
        refreshed = symbol_module.enabled_mappings(db)

        assert [row.symbol for row in first] == ["BTC"]
        assert [row.symbol for row in cached] == ["BTC"]
        assert [row.symbol for row in refreshed] == ["BTC", "ETH"]

def test_signal_rejects_unprofitable() -> None:
    signal = evaluate_signal(-1, 0.2, 5, 0.08)
    assert signal.status == "rejected"

def test_hyperliquid_symbol_map_can_include_standard_and_hip3_symbols() -> None:
    mappings = [
        SimpleNamespace(symbol="BTC", leg_a_venue="hyperliquid", leg_a_venue_symbol="BTC"),
        SimpleNamespace(symbol="JP225", leg_a_venue="hyperliquid", leg_a_venue_symbol="xyz:JP225"),
    ]

    assert hyperliquid_symbol_map(mappings, hip3_only=False) == {"BTC": "BTC", "xyz:JP225": "JP225"}
    assert hyperliquid_symbol_map(mappings, hip3_only=True) == {"xyz:JP225": "JP225"}

def test_symbol_mapping_file_seeds_missing_without_overwriting_existing(tmp_path, monkeypatch) -> None:
    mapping_file = tmp_path / "symbol_mappings.yaml"
    mapping_file.write_text(
        """
symbols:
  - symbol: BTC
    leg_a_venue_symbol: BTC
    mt5_symbol: BTCUSD
    min_order_size: 1.23
    enabled: true
  - symbol: ETH
    leg_a_venue_symbol: ETH
    mt5_symbol: ETHUSD
    min_order_size: 2.34
    enabled: true
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        symbol_module,
        "get_settings",
        lambda: SimpleNamespace(security=SimpleNamespace(symbol_mapping_path=str(mapping_file))),
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        db.add(SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTC-PERP", mt5_symbol="BTCUSD", min_order_size=0.5))
        db.commit()
        seeded = symbol_module.seed_symbol_mappings_from_file(db)
        btc = db.query(SymbolMapping).filter(SymbolMapping.symbol == "BTC").one()
        eth = db.query(SymbolMapping).filter(SymbolMapping.symbol == "ETH").one()
        assert seeded == 1
        assert btc.leg_a_venue_symbol == "BTC-PERP"
        assert btc.min_order_size == 0.5
        assert eth.min_order_size == 2.34

def test_symbol_mapping_rejects_empty_leg_a_venue_symbol() -> None:
    with pytest.raises(ValueError, match="venue symbol"):
        SymbolMappingIn(symbol="BTC", leg_a_venue_symbol="", mt5_symbol="BTCUSD", leg_a_venue="hyperliquid")

def test_mt5_points_swap_cost() -> None:
    cost = _estimate_mt5_swap_cost(swap_value=-34.2, swap_mode=1, point=0.01, contract_size=1.0, quantity=0.5, holding_days=1)
    assert cost == 0.171

def test_l2_market_fill_walks_multiple_levels() -> None:
    book = order_book_cache.put(
        "hyperliquid",
        "L2TEST",
        bids=[(100.0, 1.0), (99.0, 2.0)],
        asks=[(101.0, 1.0), (102.0, 2.0)],
        source="test",
    )

    fill = simulate_market_fill(book, "buy", 2.0)

    assert fill.enough_liquidity
    assert fill.filled_quantity == 2.0
    assert fill.average_price == 101.5
    assert fill.worst_price == 102.0

def test_hyperliquid_roundtrip_fee_and_spread() -> None:
    cost = estimate_cost(
        notional=1000,
        leg_b_bid=100,
        leg_b_ask=100,
        max_slippage_bps=0,
        quantity=0.01,
        leg_a_bid=64272,
        leg_a_ask=64273,
        leg_a_fee_rate=0.00045,
        leg_a_fee_round_trips=2,
        leg_a_funding_rate=0,
        leg_b_commission_rate=0,
        leg_b_swap_cost=0,
    )
    assert cost.leg_a_fee == 0.9
    assert cost.leg_a_spread == 0.01

def test_create_current_symbol_opportunity_uses_best_executable_direction() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(default_notional=1000.0))
    db.add(SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTCUSDT", mt5_symbol="BTCUSD", enabled=True))
    db.add(
        SpreadDirectionCurrent(
            symbol="BTC",
            direction="long_leg_a_short_leg_b",
            leg_a_bid=65000,
            leg_a_ask=65010,
            leg_b_bid=65020,
            leg_b_ask=65030,
            quantity=0.1,
            leg_a_quantity=0.1,
            leg_b_quantity=0.1,
            gross_spread=10,
            entry_spread=10,
            close_spread=5,
            unit_cost=1,
            unit_net_profit=9,
            total_cost=1,
            net_profit=9,
            annualized_return=0.1,
            status="candidate",
            reason="未达入场线",
        )
    )
    db.add(
        SpreadDirectionCurrent(
            symbol="BTC",
            direction="long_leg_b_short_leg_a",
            leg_a_bid=65000,
            leg_a_ask=65010,
            leg_b_bid=65020,
            leg_b_ask=65030,
            quantity=0.2,
            leg_a_quantity=0.2,
            leg_b_quantity=0.2,
            gross_spread=20,
            entry_spread=20,
            close_spread=6,
            unit_cost=2,
            unit_net_profit=18,
            total_cost=2,
            net_profit=18,
            annualized_return=0.2,
            status="executable",
            reason="ready",
        )
    )
    db.commit()

    opportunity = markets_api._create_current_symbol_opportunity(db, "btc", "tester")

    assert opportunity.direction == "long_leg_b_short_leg_a"
    assert opportunity.status == "executable"
    assert opportunity.entry_threshold == 20
    assert opportunity.exit_target == 6
    assert opportunity.reject_reason == "manual_execute_from_spread:tester; source_status=executable; source_reason=ready"

def test_direction_spreads_separate_entry_close_and_mid() -> None:
    long_hl = spreads_for_direction("long_leg_a_short_leg_b", leg_a_bid=99, leg_a_ask=101, leg_b_bid=110, leg_b_ask=111)
    long_mt5 = spreads_for_direction("long_leg_b_short_leg_a", leg_a_bid=99, leg_a_ask=101, leg_b_bid=110, leg_b_ask=111)

    assert long_hl.entry_spread == 9
    assert long_hl.close_spread == 12
    assert long_hl.mid_spread == 10.5
    assert long_hl.spread_cost == 3
    assert long_mt5.entry_spread == -12
    assert long_mt5.close_spread == -9
    assert long_mt5.spread_cost == 3

def test_risk_blocks_paused_mode() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        db.add(StrategySetting())
        db.add(RiskSetting(mode="paused"))
        db.add(SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTC", mt5_symbol="BTCUSD"))
        db.commit()
        decision = pre_trade_check(db, "BTC", 1000, 1, datetime.now(timezone.utc).replace(tzinfo=None))
        assert not decision.allowed

def test_scan_state_spread_dict_includes_compute_timings() -> None:
    scanner_module._scan_timings["BTC"] = {
        "symbol_scan_duration_ms": 2.5,
        "signal_duration_ms": 0.4,
        "candidate_sync_duration_ms": 0.2,
    }
    row = SimpleNamespace(
        symbol="BTC",
        status="rejected",
        __table__=SimpleNamespace(columns=[SimpleNamespace(name="symbol"), SimpleNamespace(name="status")]),
    )

    data = scanner_module._spread_state_dict(row)

    assert data["symbol"] == "BTC"
    assert data["symbol_scan_duration_ms"] == 2.5
    assert data["signal_duration_ms"] == 0.4
    assert data["candidate_sync_duration_ms"] == 0.2

def test_xyz_missing_meta_falls_back_to_growth_fee_multiplier() -> None:
    taker, maker, source = _hyperliquid_effective_fee_rates("xyz:JPY", 0.00045, 0.00015, {})
    assert taker == pytest.approx(0.00009)
    assert maker == pytest.approx(0.00003)
    assert "xyz_growth" in source

def test_local_mt5_stock_close_only_blocks_open_but_allows_close() -> None:
    mapping = SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCXz")
    apply_mt5_session_template(mapping, "stock_us_close_only")
    state = mt5_session_state(mapping, datetime(2026, 6, 23, 10, 30, tzinfo=timezone.utc))

    can_open, open_reason = mt5_action_allowed(state, "long_leg_a_short_leg_b", "open")
    can_close, close_reason = mt5_action_allowed(state, "long_leg_a_short_leg_b", "close")

    assert state.status == "reduce_only"
    assert state.session_source == "exness_template"
    assert not can_open
    assert "只平仓" in state.reason
    assert "不允许" in open_reason
    assert can_close
    assert close_reason == ""

def test_mt5_pre_close_blocks_open_but_allows_close() -> None:
    state = MT5SessionState(
        symbol="BTC",
        status="pre_close_no_open",
        reason="MT5 临近休市，禁止新开仓但允许平仓",
        can_quote=True,
        can_open_long=False,
        can_open_short=False,
        can_close_long=True,
        can_close_short=True,
    )
    can_open, open_reason = mt5_action_allowed(state, "long_leg_b_short_leg_a", "open")
    can_close, close_reason = mt5_action_allowed(state, "long_leg_b_short_leg_a", "close")
    assert not can_open
    assert "不允许" in open_reason
    assert can_close
    assert close_reason == ""

def test_xyz_growth_mode_uses_effective_fee_multiplier() -> None:
    taker, maker, source = _hyperliquid_effective_fee_rates(
        "xyz:JP225",
        0.00045,
        0.00015,
        {"xyz:JP225": {"growthMode": "enabled"}},
    )
    assert taker == pytest.approx(0.00009)
    assert maker == pytest.approx(0.00003)
    assert "xyz_growth" in source

def test_hyperliquid_l2book_message_writes_exchange_timestamp() -> None:
    manager = MarketDataManager()
    cache = QuoteCache()
    import app.workers.market_data as market_data_module

    original_worker_cache = market_data_module.quote_cache
    try:
        market_data_module.quote_cache = cache
        payload = {
            "channel": "l2Book",
            "data": {
                "coin": "xyz:JP225",
                "time": 1_782_040_271_224,
                "levels": [
                    [{"px": "72301.0", "sz": "0.00051", "n": 1}],
                    [{"px": "72499.0", "sz": "0.00048", "n": 1}],
                ],
            },
        }

        manager._handle_hyperliquid_message(payload, {"xyz:JP225": "JP225"}, "hyperliquid_l2Book_fast")

        quote = cache.latest("hyperliquid", "JP225")
        assert quote is not None
        assert quote.bid == 72301.0
        assert quote.ask == 72499.0
        assert quote.source == "hyperliquid_l2Book_fast"
        assert quote.exchange_ts == _exchange_time_from_hyperliquid_ms(1_782_040_271_224)
    finally:
        market_data_module.quote_cache = original_worker_cache

def test_scanner_liquidity_uses_l2_before_top_depth() -> None:
    order_book_cache.put(
        "hyperliquid",
        "OIL-L2",
        bids=[(73.70, 1.0), (73.69, 100.0)],
        asks=[(73.72, 1.0), (73.73, 100.0)],
        source="test",
    )

    enough = scanner_module._leg_a_liquidity_reason("OIL-L2", "sell", 70.0, 5000.0, 100.0)
    not_enough = scanner_module._leg_a_liquidity_reason("OIL-L2", "sell", 200.0, 5000.0, 100.0)

    assert enough == ""
    assert "L2 深度不足" in not_enough

def test_mt5_action_allowed_uses_mt5_leg_direction() -> None:
    state = MT5SessionState(
        symbol="BTC",
        status="open",
        reason="",
        can_quote=True,
        can_open_long=True,
        can_open_short=False,
        can_close_long=True,
        can_close_short=False,
        mt5_leg="a",
    )

    can_open_a_long, reason_a_long = mt5_action_allowed(state, "long_leg_a_short_leg_b", "open")
    can_open_a_short, reason_a_short = mt5_action_allowed(state, "long_leg_b_short_leg_a", "open")

    assert can_open_a_long
    assert reason_a_long == ""
    assert not can_open_a_short
    assert "不允许" in reason_a_short

def test_scanner_records_two_direction_current_rows(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(StrategySetting(signal_mode="fixed_profit", min_net_profit=-999, min_annualized_return=-999, default_notional=1000))
    db.add(SymbolMapping(symbol="DUAL", leg_a_venue_symbol="DUAL", mt5_symbol="DUAL", mt5_min_lot=1, mt5_volume_step=1, mt5_contract_size=1, enabled=True))
    db.commit()
    quote_cache.put("hyperliquid", "DUAL", bid=99, ask=101, depth_notional=100000, source="test")
    quote_cache.put("mt5", "DUAL", bid=110, ask=111, depth_notional=100000, source="test")
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=99, ask=101, mid=100, depth_notional=100000, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        leg_b=SimpleNamespace(bid=110, ask=111, mid=110.5, depth_notional=100000, local_recv_ts=datetime.now(timezone.utc).replace(tzinfo=None)),
        time_diff_ms=0,
    )
    monkeypatch.setattr(scanner_module.quote_synchronizer, "synchronized", lambda *args, **kwargs: (synced, ""))
    monkeypatch.setattr(scanner_module, "mt5_session_state", lambda mapping: MT5SessionState(mapping.symbol, "normal_trade", "", True, True, True, True, True))
    monkeypatch.setattr(scanner_module, "venue_cost_inputs", lambda venue, symbol: SimpleNamespace(source="test", maker_fee_rate=0, taker_fee_rate=0, funding_rate=0, funding_interval_hours=1))
    monkeypatch.setattr(scanner_module, "mt5_cost_inputs", lambda *args, **kwargs: SimpleNamespace(source="test", commission_rate=0, swap_cost=0))
    monkeypatch.setattr(scanner_module.mt5_tradability_cache, "is_fresh_allowed", lambda *args, **kwargs: (True, "ok"))

    scanner_module.clear_strategy_setting_cache()
    symbol_module.clear_symbol_mapping_cache()
    scanner_module.run_scan(db)
    state = scan_state_store.snapshot()
    assert state["ready"] is True
    assert {row["direction"] for row in state["direction_spreads"] if row["symbol"] == "DUAL"} == {"long_leg_a_short_leg_b", "long_leg_b_short_leg_a"}
    scanner_module.persist_scan_state(db)
    rows = db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol == "DUAL").all()
    current = db.query(SpreadCurrent).filter(SpreadCurrent.symbol == "DUAL").one()
    opportunity = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.symbol == "DUAL", ArbitrageOpportunity.status == "executable").first()

    assert {row.direction for row in rows} == {"long_leg_a_short_leg_b", "long_leg_b_short_leg_a"}
    assert current.entry_spread == current.gross_spread
    assert current.close_spread != current.entry_spread
    assert opportunity is not None
    assert opportunity.trigger_leg_a_bid == 99
    assert opportunity.trigger_leg_a_ask == 101
    assert opportunity.trigger_leg_b_bid == 110
    assert opportunity.trigger_leg_b_ask == 111

def test_hyperliquid_fast_l2book_subscription_includes_fast_flag() -> None:
    assert l2book_subscription("xyz:JP225", fast=True) == {"type": "l2Book", "coin": "xyz:JP225", "fast": True}
    assert l2book_subscription("BTC", fast=False) == {"type": "l2Book", "coin": "BTC"}

def test_leg_b_spread_rebate_reduces_spread_cost() -> None:
    cost = estimate_cost(
        notional=1000,
        leg_b_bid=100,
        leg_b_ask=101,
        max_slippage_bps=0,
        quantity=1,
        leg_a_bid=100,
        leg_a_ask=101,
        leg_a_fee_rate=0,
        leg_a_funding_rate=0,
        leg_b_commission_rate=0,
        leg_b_swap_cost=0,
        leg_b_spread_rebate_rate=0.2,
    )
    assert round(cost.leg_b_spread, 6) == round((1 / 100.5) * 1000 * 0.8, 6)

def test_cost_model_positive_total() -> None:
    cost = estimate_cost(1000, 64990, 65010, 8)
    assert cost.total > 0
    assert cost.leg_b_spread > 0

def test_local_mt5_quote_only_blocks_close_for_indices() -> None:
    mapping = SymbolMapping(symbol="JP225", leg_a_venue_symbol="JP225", mt5_symbol="JP225")
    apply_mt5_session_template(mapping, "index_us_jp")
    state = local_schedule_state(mapping, datetime(2026, 6, 23, 21, 30, tzinfo=timezone.utc))

    assert state is not None
    assert state.status == "quote_only"
    assert not state.can_open_long
    assert not state.can_close_long

def test_hyperliquid_short_positive_funding_reduces_cost() -> None:
    cost = estimate_cost(
        notional=1000,
        leg_b_bid=100,
        leg_b_ask=100.1,
        max_slippage_bps=0,
        quantity=0,
        leg_a_bid=0,
        leg_a_ask=0,
        leg_a_fee_rate=0,
        leg_a_funding_rate=0.001,
        leg_a_side="sell",
        leg_b_commission_rate=0,
        leg_b_swap_cost=0,
        holding_hours=1,
    )
    assert cost.leg_a_funding == -1

def test_symbol_negative_close_spread_limit_tightens_exit_target() -> None:
    mapping = SymbolMapping(symbol="SPCX", leg_a_venue_symbol="xyz:SPCX", mt5_symbol="SPCX", max_close_spread=-0.11)

    assert scanner_module._effective_exit_target(mapping, 0.047) == pytest.approx(-0.11)
    assert scanner_module._effective_exit_target(mapping, 0) == pytest.approx(-0.11)

def test_strategy_setting_cache_requires_explicit_clear() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as db:
        scanner_module.clear_strategy_setting_cache()
        db.add(StrategySetting(min_total_profit=1.0))
        db.commit()

        first = scanner_module.get_strategy_setting(db)
        row = db.query(StrategySetting).first()
        row.min_total_profit = 2.0
        db.commit()
        cached = scanner_module.get_strategy_setting(db)
        scanner_module.clear_strategy_setting_cache()
        refreshed = scanner_module.get_strategy_setting(db)

        assert first.min_total_profit == 1.0
        assert cached.min_total_profit == 1.0
        assert refreshed.min_total_profit == 2.0
