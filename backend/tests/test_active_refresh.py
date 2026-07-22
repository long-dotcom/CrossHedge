"""执行前 BBO 主动刷新测试。"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.market import active_refresh
from app.venues.domain.models import Ticker


def test_execution_refresh_uses_configured_venues_without_orderbook(monkeypatch) -> None:
    calls: list[tuple[str, str, str]] = []
    writes: list[tuple[tuple, dict]] = []
    received_at = datetime(2026, 7, 22, 10, 48, 41, tzinfo=timezone.utc)

    class Connector:
        def __init__(self, venue: str) -> None:
            self.venue = venue

        def get_ticker(self, symbol: str) -> Ticker:
            calls.append((self.venue, "ticker", symbol))
            return Ticker(self.venue, symbol, 100, 101, 2, 3, received_at=received_at)

        def get_order_book(self, symbol: str, depth: int = 20):
            raise AssertionError("执行前 BBO 刷新不应获取完整订单簿")

    class QuoteSink:
        def put(self, *args, **kwargs) -> None:
            writes.append((args, kwargs))

    monkeypatch.setattr(
        active_refresh.native_venue_manager,
        "connector_for",
        lambda venue, _mode: Connector(venue),
    )
    monkeypatch.setattr(active_refresh, "quote_cache", QuoteSink())
    mapping = SimpleNamespace(
        symbol="GOLD",
        leg_a_venue="binance",
        leg_a_symbol="XAUUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="XAUUSD",
    )

    refreshed = active_refresh.refresh_execution_quotes(mapping)

    assert refreshed == ["leg_a", "leg_b"]
    assert calls == [
        ("binance", "ticker", "XAUUSDT"),
        ("mt5", "ticker", "XAUUSD"),
    ]
    assert [row[0][0] for row in writes] == ["binance", "mt5"]
    assert all(row[1]["local_recv_ts"] == received_at for row in writes)
    assert all(row[1]["bid_depth_notional"] == 200.0 for row in writes)
    assert all(row[1]["ask_depth_notional"] == 303.0 for row in writes)
