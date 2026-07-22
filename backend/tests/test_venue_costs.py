"""按 venue 自动成本模型测试。"""

from types import SimpleNamespace

import pytest

from app.market.quotes import quote_cache
from app.market.scanner import _projected_profit, _readonly_leg_pair_payloads, _venue_fee_rate
from app.strategy.cost import estimate_pair_cost
from app.strategy.live_costs import VenueCostUnavailable, estimated_pair_close_fee, venue_cost_inputs
from app.strategy.position_sizing import calculate_position_sizing
from app.exchanges.credentials import (
    _binance_environment as http_binance_environment,
    normalize_connection_environment,
)


def test_pair_cost_counts_both_legs_open_and_close_fees_without_duplicate_spread() -> None:
    cost = estimate_pair_cost(
        notional=1000,
        leg_a_open_fee_rate=0.00045,
        leg_a_close_fee_rate=0.00045,
        leg_b_open_fee_rate=0.0002,
        leg_b_close_fee_rate=0.0005,
    )

    assert cost.leg_a_fee == pytest.approx(0.9)
    assert cost.leg_b_fee == pytest.approx(0.7)
    assert cost.leg_a_spread == 0
    assert cost.leg_b_spread == 0
    assert cost.total == pytest.approx(1.6)


def test_mt5_cost_source_is_automatic_zero_fee() -> None:
    costs = venue_cost_inputs("MT5", "BTCUSD")

    assert costs.maker_fee_rate == 0
    assert costs.taker_fee_rate == 0
    assert costs.source == "mt5_no_trading_fee"


def test_native_cost_source_uses_instrument_fees(monkeypatch) -> None:
    instrument = SimpleNamespace(
        maker_fee_rate=-0.00001,
        taker_fee_rate=0.0004,
    )
    connector = SimpleNamespace(get_instrument=lambda symbol: instrument)
    monkeypatch.setattr(
        "app.venues.manager.native_venue_manager.connector_for",
        lambda venue, mode: connector,
    )

    costs = venue_cost_inputs("binance", "TESTUSDT-PERP")

    assert costs.maker_fee_rate == pytest.approx(-0.00001)
    assert costs.taker_fee_rate == pytest.approx(0.0004)
    assert costs.source == "native_binance"


def test_gold_position_sizing_keeps_mt5_lots_separate_from_base_quantity() -> None:
    mapping = SimpleNamespace(
        mt5_currency_profit="USD",
        quote_asset="USD",
        mt5_contract_size=100.0,
        contract_multiplier=100.0,
        mt5_min_lot=0.01,
        min_order_size=1.0,
        mt5_volume_step=0.01,
    )

    sizing = calculate_position_sizing(
        mapping,
        leg_b_mid=4057.25,
        leg_a_mid=4059.155,
        target_notional_usd=1000.0,
    )

    assert sizing.leg_b_lots == pytest.approx(0.01)
    assert sizing.leg_b_base_quantity == pytest.approx(1.0)
    assert sizing.leg_a_base_quantity == pytest.approx(1.0)
    assert sizing.notional_usd == pytest.approx(4057.25)


def test_gold_position_sizing_does_not_round_target_up_to_next_mt5_lot() -> None:
    mapping = SimpleNamespace(
        mt5_currency_profit="USD",
        quote_asset="USD",
        mt5_contract_size=100.0,
        contract_multiplier=100.0,
        mt5_min_lot=0.01,
        min_order_size=1.0,
        mt5_volume_step=0.01,
    )

    sizing = calculate_position_sizing(
        mapping,
        leg_b_mid=3990.0,
        leg_a_mid=3992.0,
        target_notional_usd=4100.0,
    )

    assert sizing.leg_b_lots == pytest.approx(0.01)
    assert sizing.notional_usd == pytest.approx(3990.0)
    assert sizing.notional_usd <= 4100.0


def test_crypto_pair_position_sizing_uses_base_quantity_on_both_legs() -> None:
    mapping = SimpleNamespace(
        leg_b_venue="binance",
        min_order_size=0.001,
        quote_asset="USD",
    )

    sizing = calculate_position_sizing(
        mapping,
        leg_b_mid=4000.0,
        leg_a_mid=4002.0,
        target_notional_usd=1000.0,
    )

    assert sizing.leg_a_quantity == pytest.approx(0.25)
    assert sizing.leg_b_quantity == pytest.approx(0.25)
    assert sizing.notional_usd == pytest.approx(1000.0)


def test_readonly_scanner_cost_only_contains_spread_and_fees(monkeypatch) -> None:
    mapping = SimpleNamespace(
        symbol="GOLD-COST-TEST",
        leg_a_venue="binance",
        leg_a_symbol="XAUUSDT",
        leg_a_venue_symbol="XAUUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="XAUUSD",
        mt5_symbol="XAUUSD",
        mt5_currency_profit="USD",
        quote_asset="USD",
        mt5_contract_size=100.0,
        contract_multiplier=100.0,
        mt5_min_lot=0.01,
        min_order_size=1.0,
        mt5_volume_step=0.01,
        max_slippage_bps=8.0,
        hl_open_order_type="market",
        hl_close_order_type="market",
        hl_post_only=False,
        mt5_open_order_type="market",
        mt5_close_order_type="market",
        max_close_spread=0.0,
    )
    settings = SimpleNamespace(
        quote=SimpleNamespace(stale_ms=5000, loose_sync_ms=5000),
        cost=SimpleNamespace(default_slippage_bps=0.0, default_fx_cost_rate=0.0),
    )
    strategy = SimpleNamespace(default_notional=1000.0, max_holding_minutes=240)
    quote_cache.put("binance", mapping.symbol, 4059.15, 4059.16, 100000, "test")
    quote_cache.put("mt5", mapping.symbol, 4057.166, 4057.334, 100000, "test")
    monkeypatch.setattr(
        "app.market.scanner.venue_cost_inputs",
        lambda venue, symbol: SimpleNamespace(
            maker_fee_rate=0.0,
            taker_fee_rate=0.0004 if venue == "binance" else 0.0,
            source=f"test_{venue}",
        ),
    )

    rows = _readonly_leg_pair_payloads(mapping, settings, strategy)
    long_mt5 = next(row for row in rows if row["direction"] == "long_leg_b_short_leg_a")

    assert long_mt5["leg_b_quantity"] == pytest.approx(0.01)
    assert long_mt5["leg_a_quantity"] == pytest.approx(1.0)
    # 买卖价差已进入可执行价差；额外成本仅为 Binance 双边开平仓手续费。
    assert long_mt5["total_cost"] == pytest.approx(3.2458)

    def unavailable_costs(venue, symbol):
        raise VenueCostUnavailable(f"{venue} {symbol} unavailable")

    monkeypatch.setattr("app.market.scanner.venue_cost_inputs", unavailable_costs)
    rejected_rows = _readonly_leg_pair_payloads(mapping, settings, strategy)

    assert len(rejected_rows) == 2
    assert all(row["status"] == "rejected" for row in rejected_rows)
    assert all(row["unit_net_profit"] == 0.0 for row in rejected_rows)
    assert all("自动成本不可用" in row["reason"] for row in rejected_rows)


def test_scanner_only_uses_maker_rate_for_post_only_limit() -> None:
    costs = SimpleNamespace(maker_fee_rate=0.0001, taker_fee_rate=0.0005)

    assert _venue_fee_rate("market", costs) == pytest.approx(0.0005)
    assert _venue_fee_rate("limit", costs) == pytest.approx(0.0005)
    assert _venue_fee_rate("limit", costs, post_only=True) == pytest.approx(0.0001)


def test_projected_profit_includes_nonzero_exit_target_once() -> None:
    total, unit = _projected_profit(12.0, 3.0, 2.0, 2.0)

    assert unit == pytest.approx(8.0)
    assert total == pytest.approx(16.0)


def test_estimated_close_fee_counts_each_crypto_leg_and_keeps_mt5_zero(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.strategy.live_costs.venue_cost_inputs",
        lambda venue, symbol: SimpleNamespace(
            taker_fee_rate={"hyperliquid": 0.00045, "binance": 0.0004, "mt5": 0.0}[venue],
        ),
    )
    crypto_pair = SimpleNamespace(
        leg_a_venue="hyperliquid", leg_a_symbol="BTC",
        leg_b_venue="binance", leg_b_symbol="BTCUSDT-PERP",
    )
    mt5_pair = SimpleNamespace(
        leg_a_venue="hyperliquid", leg_a_symbol="BTC",
        leg_b_venue="mt5", leg_b_symbol="BTCUSD",
    )

    assert estimated_pair_close_fee(crypto_pair, 1000) == pytest.approx(0.85)
    assert estimated_pair_close_fee(mt5_pair, 1000) == pytest.approx(0.45)


def test_legacy_sandbox_means_live_connection_not_testnet() -> None:
    assert normalize_connection_environment("sandbox") == "live"
    assert http_binance_environment("sandbox") == "live"
    assert http_binance_environment("testnet") == "testnet"
