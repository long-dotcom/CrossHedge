"""分析测试：价差分析、资金费分析、领先-滞后分析、统计信号计算。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, ExchangeCredential, SpreadBucket, SpreadSnapshot,
    StrategySetting, SymbolMapping,
)
from app.analytics.spreads import SpreadPoint, downsample_spreads, load_spread_points, summarize_spreads
from app.analytics.funding import FundingPoint, bucket_funding_points, funding_history, summarize_funding
from app.analytics.lead_lag import lead_lag_report
from app.strategy.statistical_signal import evaluate_entry_signal, refresh_signal_stats_cache
from app.market.quotes import quote_cache
import time


def test_spread_series_downsamples_large_window() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    points = [
        SpreadPoint(created_at=now + timedelta(seconds=index), spread=float(index), total_cost=0.1, net_profit=0.0)
        for index in range(3600)
    ]
    series = downsample_spreads(points, "1h")
    assert len(series) <= 720
    assert series[0]["count"] >= 1

def test_statistical_exit_target_rejects_oversized_unit_buffer() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.90,
            auto_close_unit_profit_buffer=20,
            min_total_profit=0,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 0.08 + index * 0.001
            db.add(
                SpreadBucket(
                    symbol="OIL",
                    direction="long_leg_b_short_leg_a",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=0.03,
                    avg_unit_net_profit=spread - 0.03,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "OIL", "long_leg_b_short_leg_a", 0.115, 0.03, 0.085, 0.85, 1)
        assert signal.exit_target == 0.0

def test_spread_analytics_empty_summary() -> None:
    summary = summarize_spreads([], "1h")
    assert summary["analytics_status"] == "no_data"
    assert summary["sample_count"] == 0

def test_lead_lag_detects_following_move() -> None:
    symbol = "LLTEST"
    quote_cache.put("hyperliquid", symbol, 100, 101, 10000, "test")
    quote_cache.put("mt5", symbol, 100, 101, 10000, "test")
    time.sleep(0.001)
    quote_cache.put("hyperliquid", symbol, 102, 103, 10000, "test")
    time.sleep(0.001)
    quote_cache.put("mt5", symbol, 102, 103, 10000, "test")
    report = lead_lag_report(symbol, window_seconds=60, threshold_bps=50, follow_ratio=0.5, max_lag_ms=2000)
    summary = report["summary"]["leg_a_to_leg_b"]
    assert summary["event_count"] >= 1
    assert summary["follow_count"] >= 1

def test_statistical_signal_reads_background_refreshed_stats(monkeypatch) -> None:
    from app.strategy import statistical_signal as statistical_signal_module

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    strategy = StrategySetting(
        signal_mode="statistical",
        statistical_lookback_range="1h",
        statistical_min_samples=20,
        min_total_profit=0,
    )
    db.add(strategy)
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225", enabled=True))
    db.commit()
    points = [SpreadPoint(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=index), 100 + index, 20, 80 + index) for index in range(30)]
    calls = {"count": 0}

    def fake_load_points(db, symbol, direction, range_value):
        calls["count"] += 1
        return points

    statistical_signal_module.clear_signal_stats_cache()
    monkeypatch.setattr(statistical_signal_module, "load_spread_points", fake_load_points)
    try:
        assert refresh_signal_stats_cache(db) == 2
        monkeypatch.setattr(
            statistical_signal_module,
            "load_spread_points",
            lambda *args, **kwargs: pytest.fail("扫描热路径不应重新读取历史样本"),
        )
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 126, 20, 106, 1, 1)
    finally:
        statistical_signal_module.clear_signal_stats_cache()

    assert calls["count"] == 2
    assert signal.reachable_entry > 0

def test_funding_day_bucket_and_positive_bias() -> None:
    now = datetime(2026, 1, 1)
    points = [
        FundingPoint(time=now + timedelta(hours=index), funding_rate=0.00001 if index < 6 else -0.000005)
        for index in range(8)
    ]
    summary = summarize_funding(points, "24h")
    buckets = bucket_funding_points(points, "day")
    assert summary["bias"] == "positive"
    assert summary["positive_count"] == 6
    assert summary["negative_count"] == 2
    assert buckets[0]["sum_funding_rate"] == pytest.approx(0.00005)
    assert buckets[0]["count"] == 8

def test_statistical_exit_target_uses_low_percentile_and_profit_buffer() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.90,
            auto_close_unit_profit_buffer=20,
            min_total_profit=0,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 80 + index * 10
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_leg_a_short_leg_b",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=70,
                    avg_unit_net_profit=spread - 70,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 360, 70, 290, 10, 1)
        assert signal.exit_target == pytest.approx(152.5)
        assert signal.exit_target <= 360 - signal.cost_guard - strategy.auto_close_unit_profit_buffer

def test_spread_analytics_detects_mean_reversion_shape() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values = [1.0 + (0.4 * (0.92 ** index)) for index in range(160)]
    points = [
        SpreadPoint(created_at=now + timedelta(seconds=index * 10), spread=value, total_cost=0.1, net_profit=value - 0.1)
        for index, value in enumerate(values)
    ]
    summary = summarize_spreads(points, "1h")
    assert summary["sample_count"] == 160
    assert summary["half_life_seconds"] is not None
    assert summary["opportunity_score"] >= 0

def test_load_spread_points_supports_close_and_mid_basis() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadSnapshot

        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_leg_b_short_leg_a",
                leg_a_bid=99,
                leg_a_ask=101,
                leg_b_bid=110,
                leg_b_ask=111,
                gross_spread=9,
                entry_spread=9,
                close_spread=12,
                mid_spread=10.5,
                spread_cost=3,
                total_cost=0,
                net_profit=0,
                annualized_return=0,
                status="candidate",
                created_at=now,
            )
        )
        db.commit()

        entry = load_spread_points(db, "BTC", "long_leg_b_short_leg_a", "1h", basis="entry")
        close = load_spread_points(db, "BTC", "long_leg_b_short_leg_a", "1h", basis="close")
        mid = load_spread_points(db, "BTC", "long_leg_b_short_leg_a", "1h", basis="mid")

    assert [point.spread for point in entry] == [9]
    assert [point.spread for point in close] == [12]
    assert [point.spread for point in mid] == [10.5]

def test_statistical_signal_blocks_entry_when_samples_are_insufficient() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            min_total_profit=0.1,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(5):
            db.add(
                SpreadBucket(
                    symbol="OIL",
                    direction="long_leg_a_short_leg_b",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=0.8,
                    high_spread=0.8,
                    low_spread=0.8,
                    close_spread=0.8,
                    avg_spread=0.8,
                    avg_unit_cost=0.02,
                    avg_unit_net_profit=0.78,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "OIL", "long_leg_a_short_leg_b", 0.8, 0.02, 0.78, 50, 1)

        assert signal.result.status == "candidate"
        assert "统计样本不足" in signal.result.reason
        assert signal.reachable_entry == 0.0

def test_statistical_exit_target_uses_close_spread_distribution() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket

        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.75,
            reachable_entry_zscore=0.0,
            exit_target_percentile=0.25,
            cost_guard_percentile=0.5,
            min_total_profit=0,
        )
        for index in range(30):
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_leg_a_short_leg_b",
                    bucket_start=now - timedelta(seconds=30 - index),
                    bucket_seconds=1,
                    open_spread=100 + index,
                    high_spread=100 + index,
                    low_spread=100 + index,
                    close_spread=100 + index,
                    avg_spread=100 + index,
                    avg_entry_spread=100 + index,
                    avg_close_basis_spread=20 + index,
                    avg_unit_cost=0,
                    avg_unit_net_profit=100 + index,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 126, 0, 126, 1, 1)

    assert signal.reachable_entry > 100
    assert signal.exit_target < 30

def test_spread_analytics_uses_raw_snapshots_through_4h() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket, SpreadSnapshot

        db.add(
            SpreadBucket(
                symbol="BTC",
                direction="long_leg_b_short_leg_a",
                bucket_start=now - timedelta(minutes=10),
                bucket_seconds=5,
                open_spread=100,
                high_spread=100,
                low_spread=100,
                close_spread=100,
                avg_spread=100,
                avg_unit_cost=10,
                avg_unit_net_profit=90,
                sample_count=1,
            )
        )
        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_leg_b_short_leg_a",
                leg_a_bid=1,
                leg_a_ask=1,
                leg_b_bid=1,
                leg_b_ask=1,
                gross_spread=200,
                unit_cost=20,
                unit_net_profit=180,
                total_cost=20,
                net_profit=180,
                annualized_return=0,
                status="candidate",
                created_at=now - timedelta(minutes=5),
            )
        )
        db.commit()

        points = load_spread_points(db, "BTC", "long_leg_b_short_leg_a", "4h")

    assert [point.spread for point in points] == [200]

def test_funding_history_marks_non_hyperliquid_pair_unsupported(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)

    def fail_fetch(*args, **kwargs):
        raise AssertionError("should not fetch Hyperliquid funding for non-Hyperliquid mapping")

    monkeypatch.setattr("app.analytics.funding.fetch_funding_history", fail_fetch)
    with Session() as db:
        db.add(SymbolMapping(symbol="OKBY", leg_a_venue_symbol="BTC-USDT-SWAP", mt5_symbol="BTCUSDT", leg_a_venue="okx", leg_a_symbol="BTC-USDT-SWAP", leg_b_venue="bybit", leg_b_symbol="BTCUSDT"))
        db.commit()

        data = funding_history(db, "OKBY", "24h", "raw")

    assert data["supported"] is False
    assert data["funding_venue"] == ""
    assert data["items"] == []
    assert "没有已支持 funding" in data["source_error"]

def test_statistical_signal_reuses_stats_cache(monkeypatch) -> None:
    from app.strategy import statistical_signal as statistical_signal_module

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    strategy = StrategySetting(
        signal_mode="statistical",
        statistical_lookback_range="1h",
        statistical_min_samples=20,
        min_total_profit=0,
    )
    points = [SpreadPoint(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=index), 100 + index, 20, 80 + index) for index in range(30)]
    calls = {"count": 0}

    def fake_load_points(db, symbol, direction, range_value):
        calls["count"] += 1
        return points

    statistical_signal_module.clear_signal_stats_cache()
    monkeypatch.setattr(statistical_signal_module, "load_spread_points", fake_load_points)
    try:
        first = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 126, 20, 106, 1, 1)
        second = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 127, 20, 107, 1, 1)
    finally:
        statistical_signal_module.clear_signal_stats_cache()

    assert calls["count"] == 1
    assert first.reachable_entry == second.reachable_entry

def test_overheat_marks_risk_without_blocking_executable_entry() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.85,
            reachable_entry_zscore=1.0,
            cost_guard_percentile=0.90,
            min_total_profit=0.5,
        )
        db.add(strategy)
        db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225", min_entry_spread=200))
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 100 + index
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_leg_a_short_leg_b",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=20,
                    avg_unit_net_profit=spread - 20,
                    sample_count=1,
                )
            )
        db.commit()

        signal = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 263.1, 20, 243.1, 9.14, 1)

    mapping = SimpleNamespace(min_entry_spread=200)
    assert scanner_module._effective_entry_threshold(mapping, signal.reachable_entry) == 200
    assert signal.overheat < 200
    assert signal.result.status == "executable"
    assert "超过过热线" not in signal.result.reason
    tags = scanner_module._risk_tags(263.1, signal)
    assert tags == [
        {
            "type": "overheat",
            "message": f"价差超过过热线 {signal.overheat:.2f}",
            "value": 263.1,
            "threshold": signal.overheat,
        }
    ]

def test_spread_analytics_uses_buckets_for_24h_and_7d() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        from app.db.models import SpreadBucket, SpreadSnapshot

        db.add(
            SpreadBucket(
                symbol="BTC",
                direction="long_leg_b_short_leg_a",
                bucket_start=now - timedelta(hours=6),
                bucket_seconds=5,
                open_spread=100,
                high_spread=100,
                low_spread=100,
                close_spread=100,
                avg_spread=100,
                avg_unit_cost=10,
                avg_unit_net_profit=90,
                sample_count=1,
            )
        )
        db.add(
            SpreadSnapshot(
                symbol="BTC",
                direction="long_leg_b_short_leg_a",
                leg_a_bid=1,
                leg_a_ask=1,
                leg_b_bid=1,
                leg_b_ask=1,
                gross_spread=200,
                unit_cost=20,
                unit_net_profit=180,
                total_cost=20,
                net_profit=180,
                annualized_return=0,
                status="candidate",
                created_at=now - timedelta(hours=6),
            )
        )
        db.commit()

        points = load_spread_points(db, "BTC", "long_leg_b_short_leg_a", "7d")

    assert [point.spread for point in points] == [100]

def test_funding_history_uses_binance_funding_via_nautilus(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    captured = {}

    def fake_fetch(credential, symbol, start_ms, end_ms):
        captured["venue"] = credential.venue
        captured["symbol"] = symbol
        return [FundingPoint(time=datetime(2026, 1, 1), funding_rate=-0.0002)]

    monkeypatch.setattr("app.analytics.funding.fetch_binance_funding_history", fake_fetch)
    with Session() as db:
        db.add(ExchangeCredential(venue="binance", display_name="Binance", environment="test", enabled=True, read_only=True))
        db.add(SymbolMapping(symbol="GOLD", leg_a_venue_symbol="XAUUSDT", mt5_symbol="XAUUSD", leg_a_venue="binance", leg_a_symbol="XAUUSDT", leg_b_venue="mt5", leg_b_symbol="XAUUSD"))
        db.commit()

        data = funding_history(db, "GOLD", "24h", "raw")

    assert captured == {"venue": "binance", "symbol": "XAUUSDT"}
    assert data["funding_venue"] == "binance"
    assert data["funding_leg"] == "a"
    assert data["supported"] is True
    assert data["summary"]["latest_funding_rate"] == pytest.approx(-0.0002)

def test_funding_history_uses_hyperliquid_leg_when_not_leg_a(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    captured = {}

    def fake_fetch(coin, start_ms, end_ms):
        captured["coin"] = coin
        return [FundingPoint(time=datetime(2026, 1, 1), funding_rate=0.0001)]

    monkeypatch.setattr("app.analytics.funding.fetch_funding_history", fake_fetch)
    with Session() as db:
        db.add(SymbolMapping(symbol="REV", leg_a_venue_symbol="XAUUSD", mt5_symbol="XAUUSD", leg_a_venue="mt5", leg_a_symbol="XAUUSD", leg_b_venue="hyperliquid", leg_b_symbol="xyz:XAU"))
        db.commit()

        data = funding_history(db, "REV", "24h", "raw")

    assert captured["coin"] == "xyz:XAU"
    assert data["funding_venue"] == "hyperliquid"
    assert data["funding_leg"] == "b"
    assert data["supported"] is True
    assert data["summary"]["latest_funding_rate"] == pytest.approx(0.0001)

def test_statistical_signal_uses_reachable_entry() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with Session() as db:
        strategy = StrategySetting(
            signal_mode="statistical",
            statistical_lookback_range="1h",
            statistical_min_samples=20,
            reachable_entry_percentile=0.75,
            reachable_entry_zscore=1.0,
            cost_guard_percentile=0.90,
            min_total_profit=0.1,
        )
        db.add(strategy)
        from app.db.models import SpreadBucket

        for index in range(30):
            spread = 100 + index
            db.add(
                SpreadBucket(
                    symbol="JP225",
                    direction="long_leg_a_short_leg_b",
                    bucket_start=now + timedelta(seconds=index),
                    bucket_seconds=5,
                    open_spread=spread,
                    high_spread=spread,
                    low_spread=spread,
                    close_spread=spread,
                    avg_spread=spread,
                    avg_unit_cost=20,
                    avg_unit_net_profit=spread - 20,
                    sample_count=1,
                )
            )
        db.commit()
        signal = evaluate_entry_signal(db, strategy, "JP225", "long_leg_a_short_leg_b", 126, 20, 106, 1, 1)
        assert signal.result.status == "executable"
        assert signal.reachable_entry > 0
