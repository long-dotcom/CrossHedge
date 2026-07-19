"""Binance 原生 REST 与 Connector 测试。"""

from decimal import Decimal

import pytest

from app.venues.binance.connector import BinanceFuturesConnector
from app.venues.binance.orderbook import BinanceLocalOrderBook, OrderBookGap
from app.venues.binance.rest import BinanceApiError, BinanceFuturesRestClient, BinanceResponse
from app.venues.binance.websocket import BinanceWebSocketRuntime
from app.venues.domain.events import VenueEventType
from app.venues.domain.models import OrderRequest, OrderStatus, OrderType, PositionSide, Side


def test_native_rest_signs_request() -> None:
    calls = []

    def transport(method, url, params, headers, timeout):
        calls.append((method, url, params, headers))
        return BinanceResponse({"canTrade": True}, 200, {})

    client = BinanceFuturesRestClient(
        api_key="key",
        api_secret="secret",
        transport=transport,
    )
    payload = client.account()

    assert payload["canTrade"] is True
    assert calls[0][0:2] == ("GET", "https://fapi.binance.com/fapi/v3/account")
    assert calls[0][2]["signature"]
    assert calls[0][3]["X-MBX-APIKEY"] == "key"


def test_order_transport_failure_is_unknown_and_must_not_be_retried() -> None:
    def transport(*_args):
        raise OSError("timeout")

    client = BinanceFuturesRestClient(api_key="key", api_secret="secret", transport=transport)

    with pytest.raises(BinanceApiError) as captured:
        client.place_order({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"})

    assert captured.value.outcome_unknown is True


def test_public_market_subscription_does_not_enable_private_user_stream(monkeypatch) -> None:
    rest = BinanceFuturesRestClient(api_key="key", api_secret="secret")
    runtime = BinanceWebSocketRuntime(rest)
    monkeypatch.setattr(runtime, "start", lambda: None)

    runtime.add_symbols(["BTCUSDT"])
    assert runtime._private_enabled is False

    runtime.add_event_handler(lambda event: None)
    assert runtime._private_enabled is True


class FakeBinanceRest:
    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }
            ]
        }

    def commission_rate(self, symbol):
        return {"makerCommissionRate": "0.0002", "takerCommissionRate": "0.0005"}

    def premium_index(self, symbol):
        return {"lastFundingRate": "0.0001", "nextFundingTime": 1_800_000_000_000}

    def place_order(self, params):
        assert params["positionSide"] == "LONG"
        assert params["timeInForce"] == "GTX"
        return {
            "symbol": params["symbol"],
            "clientOrderId": params["newClientOrderId"],
            "orderId": 123,
            "status": "NEW",
            "side": params["side"],
            "type": params["type"],
            "origQty": params["quantity"],
            "executedQty": "0",
            "price": params["price"],
            "positionSide": params["positionSide"],
        }


def test_connector_maps_instrument_and_hedge_mode_order() -> None:
    connector = BinanceFuturesConnector(rest_client=FakeBinanceRest(), read_only=False)
    instrument = connector.get_instrument("BTCUSDT")
    order = connector.submit_order(
        OrderRequest(
            venue="binance",
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            client_order_id="native-binance-1",
            order_type=OrderType.LIMIT,
            price=Decimal("60000.1"),
            post_only=True,
            position_side=PositionSide.LONG,
        )
    )

    assert instrument.quantity_step == Decimal("0.001")
    assert instrument.price_tick == Decimal("0.10")
    assert instrument.maker_fee_rate == Decimal("0.0002")
    assert order.status == OrderStatus.ACCEPTED
    assert order.client_order_id == "native-binance-1"


def test_read_only_connector_rejects_order_before_network_call() -> None:
    connector = BinanceFuturesConnector(rest_client=FakeBinanceRest(), read_only=True)
    with pytest.raises(PermissionError, match="只读"):
        connector.submit_order(
            OrderRequest(
                venue="binance",
                symbol="BTCUSDT",
                side=Side.BUY,
                quantity=Decimal("0.01"),
                client_order_id="blocked",
            )
        )


def test_local_order_book_applies_sequence_and_detects_gap() -> None:
    book = BinanceLocalOrderBook("BTCUSDT")
    book.initialize(
        {
            "lastUpdateId": 100,
            "bids": [["100", "2"], ["99", "1"]],
            "asks": [["101", "2"]],
        }
    )

    assert book.apply({"U": 99, "u": 101, "pu": 98, "b": [["100", "3"]], "a": []}) is True
    assert book.apply({"U": 102, "u": 102, "pu": 101, "b": [], "a": [["101", "0"], ["102", "4"]]}) is True
    snapshot = book.snapshot()

    assert snapshot.synchronized is True
    assert snapshot.bids[0] == (Decimal("100"), Decimal("3"))
    assert snapshot.asks[0] == (Decimal("102"), Decimal("4"))
    with pytest.raises(OrderBookGap, match="序列中断"):
        book.apply({"U": 104, "u": 104, "pu": 103, "b": [], "a": []})


def test_private_order_update_emits_fast_fill_and_order_events() -> None:
    rest = BinanceFuturesRestClient(transport=lambda *_: BinanceResponse({}, 200, {}))
    runtime = BinanceWebSocketRuntime(rest)
    payload = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1_700_000_000_100,
        "T": 1_700_000_000_000,
        "o": {
            "s": "BTCUSDT",
            "c": "cid-1",
            "i": 42,
            "S": "BUY",
            "o": "LIMIT",
            "q": "0.02",
            "z": "0.01",
            "l": "0.01",
            "L": "60000",
            "ap": "60000",
            "p": "59999",
            "X": "PARTIALLY_FILLED",
            "x": "TRADE",
            "t": 99,
            "n": "0.1",
            "N": "USDT",
            "m": True,
            "ps": "LONG",
        },
    }

    events = runtime.process_private_message(payload)

    assert [event.event_type for event in events] == [
        VenueEventType.ORDER_PARTIALLY_FILLED,
        VenueEventType.FILL,
    ]
    assert events[1].fill.trade_id == "99"
    assert events[1].fill.quantity == Decimal("0.01")


def test_trade_lite_is_normalized_as_fill_event() -> None:
    rest = BinanceFuturesRestClient(transport=lambda *_: BinanceResponse({}, 200, {}))
    runtime = BinanceWebSocketRuntime(rest)

    events = runtime.process_private_message(
        {
            "e": "TRADE_LITE",
            "E": 1_700_000_000_100,
            "T": 1_700_000_000_000,
            "s": "ETHUSDT",
            "q": "0.5",
            "p": "3500",
            "S": "SELL",
            "c": "fast-cid",
            "i": 11,
            "t": 12,
            "m": False,
        }
    )

    assert len(events) == 1
    assert events[0].event_type == VenueEventType.FILL
    assert events[0].fill.client_order_id == "fast-cid"


def test_account_update_is_normalized_and_cached() -> None:
    rest = BinanceFuturesRestClient(transport=lambda *_: BinanceResponse({}, 200, {}))
    runtime = BinanceWebSocketRuntime(rest)

    events = runtime.process_private_message({
        "e": "ACCOUNT_UPDATE", "E": 1_700_000_000_100, "T": 1_700_000_000_000,
        "a": {
            "B": [{"a": "USDT", "wb": "100", "cw": "80"}],
            "P": [{"s": "BTCUSDT", "pa": "-0.01", "ep": "60000", "up": "2", "ps": "SHORT"}],
        },
    })

    assert [event.event_type for event in events] == [VenueEventType.ACCOUNT, VenueEventType.POSITION]
    assert runtime.account().available_balance == Decimal("80")
    assert runtime.positions()[0].position_side == PositionSide.SHORT
