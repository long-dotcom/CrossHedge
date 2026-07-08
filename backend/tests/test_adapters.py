"""适配器测试：Paper / Hyperliquid / MT5 / Nautilus 适配器的下单、持仓读取、探针逻辑。"""

import json
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter, mt5_demo_order_check, mt5_market_order_check
from app.adapters.nautilus import NautilusAdapter, NautilusReadOnlyAdapter
from app.adapters.nautilus_runtime import NautilusTradeMode, NautilusVenueRuntime
from app.adapters.venue import build_market_adapter, is_native_pair
from app.execution.gateway import AdapterExecutionGateway
from app.execution.engine import _execution_adapters
from app.config.settings import HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_TESTNET_INFO_URL, hyperliquid_execution_info_url
from app.market.quotes import quote_cache
from app.market.orderbook import order_book_cache
from app.exchanges.credentials import binance_futures_probe_order
from app.exchanges.credentials import _binance_live_quantity
from app.db.models import Base, ExchangeCredential, SymbolMapping, SystemSetting


def test_execution_adapters_db_probe_switch_overrides_legacy_hyperliquid_env(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="paper_live_probe_enabled", value="false"))
    db.commit()
    monkeypatch.setattr(
        "app.execution.engine.get_settings",
        lambda: SimpleNamespace(
            hyperliquid_paper_live_order_enabled=True,
            paper_live_probe_enabled=False,
            paper_live_probe_venues="*",
        ),
    )

    leg_a, _ = _execution_adapters(live=False, simulated=True, db=db)

    assert leg_a.platform == "hyperliquid"
    assert leg_a.live is False
    assert leg_a.paper_price_probe is False

def test_mt5_live_order_requires_explicit_switch() -> None:
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(mt5_live_order_enabled=False)
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert not result.success
    assert "开关未开启" in result.error_message

def test_mt5_live_reduce_only_uses_position_ticket(monkeypatch) -> None:
    sent_requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        POSITION_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_FILLING_FOK = 4
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return [SimpleNamespace(ticket=555, symbol="USOIL", type=self.POSITION_TYPE_SELL, volume=0.02)]

        def order_send(self, request):
            sent_requests.append(request)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=12345, volume=request["volume"], price=request["price"], comment="done")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.02, venue_symbol="USOIL", reduce_only=True))

    assert result.success
    assert sent_requests[0]["position"] == 555
    assert sent_requests[0]["type"] == FakeMT5.ORDER_TYPE_BUY

def test_execution_adapters_db_probe_switch_applies_to_all_non_mt5_venues(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemSetting(key="paper_live_probe_enabled", value="true"))
    db.commit()
    monkeypatch.setattr(
        "app.execution.engine.get_settings",
        lambda: SimpleNamespace(
            hyperliquid_paper_live_order_enabled=False,
            paper_live_probe_enabled=False,
            paper_live_probe_venues="hyperliquid",
        ),
    )
    mapping = SymbolMapping(
        symbol="ETH",
        leg_a_venue="okx",
        leg_a_symbol="ETH-USDT-SWAP",
        leg_a_venue_symbol="ETH-USDT-SWAP",
        leg_b_venue="mt5",
        leg_b_symbol="ETHUSD",
        mt5_symbol="ETHUSD",
    )

    leg_a, leg_b = _execution_adapters(live=False, simulated=True, mapping=mapping, db=db)

    assert leg_a.platform == "okx"
    assert leg_a.live is True
    assert leg_a.paper_price_probe is True
    assert leg_b.demo is True

def test_mt5_demo_requires_explicit_switch_before_import() -> None:
    adapter = MT5Adapter(demo=True)
    adapter.settings = SimpleNamespace(mt5_demo_order_enabled=False)
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert not result.success
    assert "demo 下单开关未开启" in result.error_message

def test_hyperliquid_live_positions_read_clearinghouse_state(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "OIL",
                                "szi": "2.5",
                                "entryPx": "76.1",
                                "markPx": "76.4",
                                "unrealizedPnl": "0.75",
                                "marginUsed": "12.3",
                                "liquidationPx": "40",
                            }
                        },
                        {"position": {"coin": "BTC", "szi": "0"}},
                    ]
                }
            ).encode("utf-8")

    calls = []

    def fake_urlopen(req, timeout):
        calls.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    )

    positions = adapter.get_positions()

    assert calls == [
        {"type": "allMids"},
        {"type": "clearinghouseState", "user": "0xabc"},
    ]
    assert positions == [
        {
            "platform": "hyperliquid",
            "symbol": "OIL",
            "side": "long",
            "quantity": 2.5,
            "entry_price": 76.1,
            "mark_price": 76.4,
            "unrealized_pnl": 0.75,
            "margin_used": 12.3,
            "liquidation_price": 40.0,
        }
    ]

def test_mt5_live_reduce_only_rejects_oversized_close(monkeypatch) -> None:
    class FakeMT5:
        ORDER_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return [SimpleNamespace(ticket=555, symbol="USOIL", type=self.POSITION_TYPE_SELL, volume=0.02)]

        def order_send(self, request):
            raise AssertionError("reduce-only 超过持仓数量时不应发单")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.05, venue_symbol="USOIL", reduce_only=True))

    assert not result.success
    assert "超过持仓" in result.error_message

def test_hyperliquid_paper_live_probe_uses_minimum_real_order_and_paper_quantity(monkeypatch) -> None:
    submitted = []

    class FakeExchange:
        def market_open(self, name, is_buy, sz, px, slippage):
            submitted.append((name, is_buy, sz, px, slippage))
            return {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": str(sz), "avgPx": "100.5", "oid": 12345}},
                        ]
                    }
                },
            }

    adapter = HyperliquidAdapter(live=True)
    adapter.paper_price_probe = True
    adapter.settings = SimpleNamespace(
        hyperliquid_paper_live_order_enabled=True,
        hyperliquid_account_address="0xabc",
        hyperliquid_secret_key="0xkey",
        hyperliquid_default_min_notional=10.0,
        hyperliquid_paper_live_slippage=0.01,
        hyperliquid_info_url="https://example.test/info",
    )
    adapter._post_info = lambda payload: (
        {"universe": [{"name": "BTC", "szDecimals": 5}]} if payload["type"] == "meta" else {"BTC": "65000"}
    )
    adapter._fee_rate = lambda order: 0.001
    monkeypatch.setattr("app.adapters.hyperliquid._load_hyperliquid_exchange", lambda settings: FakeExchange())

    result = adapter.place_order(AdapterOrder(platform="hyperliquid", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTC"))

    assert submitted == [("BTC", True, 0.00016, None, 0.01)]
    assert result.success
    assert result.external_order_id == "12345"
    assert result.filled_quantity == 0.25
    assert result.average_price == 100.5
    assert result.fee == pytest.approx(0.25 * 100.5 * 0.001)
    assert "探针真实成交量" in result.error_message

def test_execution_adapters_enable_generic_paper_live_probe_for_configured_venue(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.execution.engine.get_settings",
        lambda: SimpleNamespace(
            hyperliquid_paper_live_order_enabled=False,
            paper_live_probe_enabled=True,
            paper_live_probe_venues="binance,okx",
        ),
    )
    mapping = SymbolMapping(
        symbol="BTC",
        leg_a_venue="binance",
        leg_a_symbol="BTCUSDT",
        leg_a_venue_symbol="BTCUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="BTCUSD",
        mt5_symbol="BTCUSD",
    )

    leg_a, leg_b = _execution_adapters(live=False, simulated=True, mapping=mapping)

    assert leg_a.platform == "binance"
    assert leg_a.live is True
    assert leg_a.paper_price_probe is True
    assert getattr(leg_a, "simulated") is True
    assert leg_b.demo is True

def test_build_market_adapter_routes_non_native_venues_to_nautilus() -> None:
    assert isinstance(build_market_adapter("mt5"), MT5Adapter)
    assert isinstance(build_market_adapter("hyperliquid"), HyperliquidAdapter)
    assert isinstance(build_market_adapter("okx"), NautilusAdapter)

def test_nautilus_runtime_rejects_unsupported_venue(monkeypatch) -> None:
    monkeypatch.setattr("app.adapters.nautilus_runtime._nautilus_import_error", lambda: "")
    row = ExchangeCredential(venue="okx", environment="test", enabled=True, read_only=False, encrypted_credentials="x")
    runtime = NautilusVenueRuntime(row)

    with pytest.raises(RuntimeError, match="尚未接入 Nautilus live runtime"):
        runtime.ensure_loaded()

def test_nautilus_runtime_probe_preserves_paper_quantity(monkeypatch) -> None:
    from decimal import Decimal

    calls = []

    def fake_submit(self, order, instrument, quantity):
        calls.append((str(instrument.id), quantity))
        return SimpleNamespace(client_order_id="probe-1")

    monkeypatch.setattr("app.adapters.nautilus_runtime._nautilus_import_error", lambda: "")
    monkeypatch.setattr("app.adapters.nautilus_runtime.decrypt_credentials", lambda row: {"api_key": "k", "api_secret": "s"})
    monkeypatch.setattr("app.adapters.nautilus_runtime.NautilusVenueRuntime.ensure_loaded", lambda self: setattr(self, "status", "running"))
    monkeypatch.setattr("app.adapters.nautilus_runtime.NautilusVenueRuntime._submit_order", fake_submit)
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.instrument",
        lambda self, symbol: SimpleNamespace(
            id="BTCUSDT-PERP.BINANCE",
            size_increment=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("100"),
        ),
    )
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.get_ticker",
        lambda self, symbol: {"bid": 65000.0, "ask": 65020.0, "depth_notional": 100000.0},
    )
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.get_trades",
        lambda self, order_id: [{"client_order_id": order_id, "quantity": "0.002", "price": "65010"}],
    )
    row = ExchangeCredential(venue="binance", environment="test", enabled=True, read_only=False, encrypted_credentials="x")
    runtime = NautilusVenueRuntime(row)

    result = runtime.place_order(
        AdapterOrder(platform="binance", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTCUSDT"),
        NautilusTradeMode.PAPER_PROBE,
        configured_min_base_size=0.002,
    )

    assert result.success
    assert result.filled_quantity == 0.25
    assert result.average_price == 65010.0
    assert calls == [("BTCUSDT-PERP.BINANCE", Decimal("0.002"))]
    assert "paper 账本成交量 0.25" in result.error_message

def test_nautilus_runtime_live_order_uses_live_path(monkeypatch) -> None:
    from decimal import Decimal

    calls = []

    def fake_submit(self, order, instrument, quantity):
        calls.append((str(instrument.id), quantity, order.order_type))
        return SimpleNamespace(client_order_id="live-1")

    monkeypatch.setattr("app.adapters.nautilus_runtime._nautilus_import_error", lambda: "")
    monkeypatch.setattr("app.adapters.nautilus_runtime.decrypt_credentials", lambda row: {"api_key": "k", "api_secret": "s"})
    monkeypatch.setattr("app.adapters.nautilus_runtime.NautilusVenueRuntime.ensure_loaded", lambda self: setattr(self, "status", "running"))
    monkeypatch.setattr("app.adapters.nautilus_runtime.NautilusVenueRuntime._submit_order", fake_submit)
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.instrument",
        lambda self, symbol: SimpleNamespace(
            id="BTCUSDT-PERP.BINANCE",
            size_increment=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            min_notional=Decimal("10"),
        ),
    )
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.get_ticker",
        lambda self, symbol: {"bid": 65000.0, "ask": 65020.0, "depth_notional": 100000.0},
    )
    monkeypatch.setattr(
        "app.adapters.nautilus_runtime.NautilusVenueRuntime.get_trades",
        lambda self, order_id: [{"client_order_id": order_id, "quantity": "0.25", "price": "65020"}],
    )
    row = ExchangeCredential(venue="binance", environment="test", enabled=True, read_only=False, encrypted_credentials="x")
    runtime = NautilusVenueRuntime(row)

    result = runtime.place_order(
        AdapterOrder(platform="binance", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTCUSDT", order_type="market"),
        NautilusTradeMode.LIVE,
    )

    assert result.success
    assert result.filled_quantity == 0.25
    assert result.average_price == 65020.0
    assert calls == [("BTCUSDT-PERP.BINANCE", Decimal("0.25"), "market")]

def test_binance_live_quantity_rounds_down_without_expanding_order() -> None:
    from decimal import Decimal

    quantity = _binance_live_quantity(
        {"step": Decimal("0.001"), "min_qty": Decimal("0.001"), "min_notional": Decimal("10")},
        Decimal("0.2509"),
        Decimal("65000"),
    )

    assert quantity == Decimal("0.250")

def test_binance_nautilus_probe_uses_exchange_minimum_and_paper_quantity(monkeypatch) -> None:
    from app.exchanges.credentials import binance_futures_probe_order

    submitted = []
    fake_nautilus = SimpleNamespace(core=SimpleNamespace(nautilus_pyo3=SimpleNamespace(HttpMethod=SimpleNamespace(POST="POST"))))
    monkeypatch.setitem(sys.modules, "nautilus_trader", fake_nautilus)
    monkeypatch.setitem(sys.modules, "nautilus_trader.core", fake_nautilus.core)
    monkeypatch.setitem(sys.modules, "nautilus_trader.core.nautilus_pyo3", fake_nautilus.core.nautilus_pyo3)

    class FakeClient:
        async def sign_request(self, http_method, url_path, payload=None, ratelimiter_keys=None):
            submitted.append((str(http_method), url_path, payload, ratelimiter_keys))
            return json.dumps({"orderId": 987, "status": "FILLED", "executedQty": payload["quantity"], "avgPrice": "65010"}).encode("utf-8")

    class FakeMarketApi:
        client = FakeClient()
        base_endpoint = "/fapi/v1/"

        async def query_futures_exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "quantityPrecision": 3,
                        "filters": [
                            {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "100"},
                        ],
                    }
                ]
            }

    async def fake_ticker(row, symbol):
        return SimpleNamespace(bidPrice="65000", askPrice="65020")

    monkeypatch.setattr("app.exchanges.credentials._nautilus_binance_futures_apis", lambda row: (None, FakeMarketApi()))
    monkeypatch.setattr("app.exchanges.credentials._nautilus_binance_futures_ticker", fake_ticker)
    row = ExchangeCredential(venue="binance", environment="test", enabled=True, read_only=False, encrypted_credentials="x")

    result = binance_futures_probe_order(
        row,
        AdapterOrder(platform="binance", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTCUSDT"),
        configured_min_base_size=0.0,
    )

    assert result.success
    assert result.external_order_id == "987"
    assert result.filled_quantity == 0.25
    assert result.average_price == 65010.0
    assert submitted[0][2]["quantity"] == "0.002"
    assert submitted[0][2]["newOrderRespType"] == "RESULT"
    assert "paper 账本成交量 0.25" in result.error_message

def test_hyperliquid_live_positions_read_hip3_dex_positions(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            if self.payload.get("type") == "allMids":
                return json.dumps({"xyz:JP225": "72015"}).encode("utf-8")
            dex = self.payload.get("dex")
            positions = []
            if dex == "xyz":
                positions = [
                    {
                        "position": {
                            "coin": "xyz:JP225",
                            "szi": "0.0002",
                            "entryPx": "71875",
                            "unrealizedPnl": "0.03",
                            "marginUsed": "0.75",
                            "liquidationPx": "70034",
                        }
                    }
                ]
            return json.dumps({"assetPositions": positions}).encode("utf-8")

    calls = []

    def fake_urlopen(req, timeout):
        payload = json.loads(req.data.decode("utf-8"))
        calls.append(payload)
        return FakeResponse(payload)

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    )

    positions = adapter.get_positions(dexes=["xyz"])

    assert calls == [
        {"type": "allMids"},
        {"type": "allMids", "dex": "xyz"},
        {"type": "clearinghouseState", "user": "0xabc"},
        {"type": "clearinghouseState", "user": "0xabc", "dex": "xyz"},
    ]
    assert positions == [
        {
            "platform": "hyperliquid",
            "symbol": "xyz:JP225",
            "side": "long",
            "quantity": 0.0002,
            "entry_price": 71875.0,
            "mark_price": 72015.0,
            "unrealized_pnl": 0.03,
            "margin_used": 0.75,
            "liquidation_price": 70034.0,
        }
    ]

def test_hyperliquid_execution_info_url_uses_configured_url() -> None:
    settings = SimpleNamespace(hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL)

    assert hyperliquid_execution_info_url(settings) == HYPERLIQUID_MAINNET_INFO_URL

def test_hyperliquid_paper_live_probe_trusts_runtime_adapter_switch(monkeypatch) -> None:
    submitted = []

    class FakeExchange:
        def market_open(self, name, is_buy, sz, px, slippage):
            submitted.append((name, is_buy, sz))
            return {
                "status": "ok",
                "response": {
                    "data": {
                        "statuses": [
                            {"filled": {"totalSz": str(sz), "avgPx": "100.5", "oid": 67890}},
                        ]
                    }
                },
            }

    adapter = HyperliquidAdapter(live=True)
    adapter.paper_price_probe = True
    adapter.settings = SimpleNamespace(
        hyperliquid_paper_live_order_enabled=False,
        paper_live_probe_enabled=False,
        hyperliquid_account_address="0xabc",
        hyperliquid_secret_key="0xkey",
        hyperliquid_default_min_notional=10.0,
        hyperliquid_paper_live_slippage=0.01,
        hyperliquid_info_url="https://example.test/info",
    )
    adapter._post_info = lambda payload: (
        {"universe": [{"name": "BTC", "szDecimals": 5}]} if payload["type"] == "meta" else {"BTC": "65000"}
    )
    adapter._fee_rate = lambda order: 0.0
    monkeypatch.setattr("app.adapters.hyperliquid._load_hyperliquid_exchange", lambda settings: FakeExchange())

    result = adapter.place_order(AdapterOrder(platform="hyperliquid", symbol="BTC", side="buy", quantity=0.25, venue_symbol="BTC"))

    assert submitted == [("BTC", True, 0.00016)]
    assert result.success
    assert result.external_order_id == "67890"

def test_hyperliquid_live_positions_use_execution_info_url(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"assetPositions": []}).encode("utf-8")

    urls = []

    def fake_urlopen(req, timeout):
        urls.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", fake_urlopen)
    adapter = HyperliquidAdapter(live=True)
    adapter.settings = SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url=HYPERLIQUID_MAINNET_INFO_URL,
    )

    assert adapter.get_positions() == []
    assert urls == [HYPERLIQUID_MAINNET_INFO_URL, HYPERLIQUID_MAINNET_INFO_URL]

def test_mt5_live_market_order_maps_order_send(monkeypatch) -> None:
    sent_requests = []

    class FakeMT5:
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_FILLING_FOK = 4
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return symbol == "USOIL" and enabled

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def order_send(self, request):
            sent_requests.append(request)
            return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=12345, deal=67890, volume=request["volume"], price=request["price"], comment="done")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )
    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.01, venue_symbol="USOIL"))
    assert result.success
    assert result.external_order_id == "12345"
    assert result.filled_quantity == 0.01
    assert result.average_price == 76.46
    assert sent_requests[0]["symbol"] == "USOIL"

def test_mt5_live_reduce_only_rejects_without_matching_position(monkeypatch) -> None:
    class FakeMT5:
        ORDER_TYPE_BUY = 0
        POSITION_TYPE_SELL = 1
        TRADE_ACTION_DEAL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1

        def initialize(self, **kwargs):
            return True

        def last_error(self):
            return (0, "")

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return SimpleNamespace(bid=76.44, ask=76.46)

        def symbol_info(self, symbol):
            return SimpleNamespace(filling_mode=0)

        def positions_get(self, symbol=None):
            return []

        def order_send(self, request):
            raise AssertionError("reduce-only 没有持仓时不应发单")

    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
    adapter = MT5Adapter(live=True)
    adapter.settings = SimpleNamespace(
        mt5_live_order_enabled=True,
        mt5_login="",
        mt5_password="",
        mt5_server="",
        mt5_order_deviation_points=20,
        mt5_order_magic=260620,
    )

    result = adapter.place_order(AdapterOrder(platform="mt5", symbol="OIL", side="buy", quantity=0.02, venue_symbol="USOIL", reduce_only=True))

    assert not result.success
    assert "reduce-only" in result.error_message

def test_execution_adapters_enable_hyperliquid_probe_only_for_paper_switch(monkeypatch) -> None:
    monkeypatch.setattr("app.execution.engine.get_settings", lambda: SimpleNamespace(hyperliquid_paper_live_order_enabled=True))

    hl, mt5 = _execution_adapters(live=False, simulated=True)

    assert hl.live is True
    assert hl.paper_price_probe is True
    assert getattr(hl, "simulated") is True
    assert mt5.demo is True

def test_mt5_demo_order_check_requires_demo_account_and_configured_identity() -> None:
    class FakeMT5:
        ACCOUNT_TRADE_MODE_DEMO = 0

        def __init__(self, trade_mode=0, login=123, server="broker-demo") -> None:
            self.info = SimpleNamespace(trade_mode=trade_mode, login=login, server=server)

        def account_info(self):
            return self.info

        def last_error(self):
            return (0, "")

    settings = SimpleNamespace(mt5_demo_order_enabled=True, mt5_login="123", mt5_server="broker-demo")
    assert mt5_demo_order_check(FakeMT5(), settings).allowed
    assert not mt5_demo_order_check(FakeMT5(trade_mode=2), settings).allowed
    assert "不是 DEMO" in mt5_demo_order_check(FakeMT5(trade_mode=2), settings).message
    assert not mt5_demo_order_check(FakeMT5(login=999), settings).allowed
    assert not mt5_demo_order_check(FakeMT5(server="broker-real"), settings).allowed

def test_nautilus_probe_rejects_until_real_order_adapter_exists() -> None:
    adapter = NautilusReadOnlyAdapter("okx", live=True)
    adapter.paper_price_probe = True

    result = adapter.place_order(AdapterOrder(platform="okx", symbol="BTC", side="buy", quantity=1.0, venue_symbol="BTC-USDT-SWAP"))

    assert not result.success
    assert result.status == "rejected"
    assert "okx 交易所配置" in result.error_message

def test_mt5_order_check_falls_back_when_filling_mode_unsupported(monkeypatch) -> None:
    attempts = []

    class FakeResult:
        def __init__(self, retcode: int, comment: str) -> None:
            self.retcode = retcode
            self.comment = comment

    class FakeTick:
        ask = 100.1
        bid = 100.0

    class FakeInfo:
        filling_mode = 0

    class FakeMT5:
        TRADE_ACTION_DEAL = 1
        ORDER_TYPE_BUY = 0
        ORDER_TYPE_SELL = 1
        ORDER_TIME_GTC = 0
        ORDER_FILLING_IOC = 1
        ORDER_FILLING_RETURN = 2
        ORDER_FILLING_FOK = 4
        TRADE_RETCODE_INVALID_FILL = 10030
        TRADE_RETCODE_DONE = 10009
        TRADE_RETCODE_DONE_PARTIAL = 10010
        TRADE_RETCODE_PLACED = 10008

        def initialize(self, **kwargs):
            return True

        def symbol_select(self, symbol, enabled):
            return True

        def symbol_info_tick(self, symbol):
            return FakeTick()

        def symbol_info(self, symbol):
            return FakeInfo()

        def order_check(self, request):
            attempts.append(request["type_filling"])
            if request["type_filling"] == self.ORDER_FILLING_IOC:
                return FakeResult(10030, "Unsupported filling mode")
            return FakeResult(0, "Done")

        def last_error(self):
            return (0, "")

    monkeypatch.setattr("app.adapters.mt5.get_settings", lambda: SimpleNamespace(mt5_login="", mt5_password="", mt5_server="", mt5_order_deviation_points=20, mt5_order_magic=202402))
    monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())

    result = mt5_market_order_check("JP225", "buy", 1.0)

    assert result.allowed
    assert attempts[:2] == [FakeMT5.ORDER_FILLING_IOC, FakeMT5.ORDER_FILLING_RETURN]
    assert "filling=2" in result.message
