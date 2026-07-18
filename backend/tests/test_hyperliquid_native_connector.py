"""Hyperliquid 原生 Connector 和用户事件测试。"""

from decimal import Decimal

from app.venues.domain.events import VenueEventType
from app.venues.domain.models import OrderRequest, OrderStatus, OrderType, Side
from app.venues.hyperliquid.connector import HyperliquidConnector
from app.venues.hyperliquid.websocket import HyperliquidWebSocketRuntime


class FakeHyperInfo:
    def __call__(self, url, payload):
        kind = payload["type"]
        if kind == "metaAndAssetCtxs":
            return [
                {"universe": [{"name": "BTC", "szDecimals": 5, "isDelisted": False}]},
                [{"funding": "0.0001"}],
            ]
        if kind == "userFees":
            return {"userAddRate": "0.0001", "userCrossRate": "0.0004"}
        if kind == "clearinghouseState":
            return {
                "marginSummary": {"accountValue": "1000", "totalMarginUsed": "100"},
                "withdrawable": "800",
                "assetPositions": [
                    {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "60000", "unrealizedPnl": "5", "marginUsed": "100"}}
                ],
            }
        if kind == "spotClearinghouseState":
            return {"balances": [{"coin": "USDC", "total": "50", "hold": "10"}]}
        if kind == "allMids":
            return {"BTC": "60500"}
        if kind == "l2Book":
            return {
                "coin": "BTC",
                "time": 1_700_000_000_000,
                "levels": [
                    [{"px": "60499", "sz": "2"}],
                    [{"px": "60501", "sz": "3"}],
                ],
            }
        raise AssertionError(f"unexpected payload: {payload}")


class FakeExchange:
    def order(self, name, is_buy, size, price, order_type, **kwargs):
        assert name == "BTC"
        assert is_buy is True
        assert order_type == {"limit": {"tif": "Alo"}}
        assert kwargs["cloid"].to_raw().startswith("0x")
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": 123}}]}},
        }


def test_hyperliquid_native_account_instrument_and_order() -> None:
    connector = HyperliquidConnector(
        credentials={"account_address": "0xabc", "secret_key": "unused-by-factory"},
        read_only=False,
        info_transport=FakeHyperInfo(),
        exchange_factory=FakeExchange,
    )

    account = connector.get_account()
    positions = connector.get_positions()
    instrument = connector.get_instrument("BTC")
    order = connector.submit_order(
        OrderRequest(
            venue="hyperliquid",
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            client_order_id="hl-native-1",
            order_type=OrderType.LIMIT,
            price=Decimal("60000"),
            post_only=True,
        )
    )

    assert account.equity == Decimal("1000")
    assert account.balances[0].available_balance == Decimal("40")
    assert positions[0].mark_price == Decimal("60500")
    assert instrument.quantity_step == Decimal("0.00001")
    assert instrument.maker_fee_rate == Decimal("0.0001")
    assert order.status == OrderStatus.ACCEPTED
    assert order.venue_order_id == "123"


def test_hyperliquid_l2_and_private_events_are_normalized() -> None:
    runtime = HyperliquidWebSocketRuntime(ws_url="wss://example", account_address="0xabc")
    runtime.register_client_order_id("0x11111111111111111111111111111111", "business-id")
    runtime.process_message(
        {
            "channel": "l2Book",
            "data": {
                "coin": "BTC",
                "time": 1_700_000_000_000,
                "levels": [
                    [{"px": "60000", "sz": "1"}],
                    [{"px": "60001", "sz": "2"}],
                ],
            },
        }
    )
    order_events = runtime.process_message(
        {
            "channel": "orderUpdates",
            "data": [
                {
                    "order": {
                        "coin": "BTC",
                        "side": "B",
                        "limitPx": "60000",
                        "sz": "0.01",
                        "origSz": "0.02",
                        "oid": 123,
                        "cloid": "0x11111111111111111111111111111111",
                    },
                    "status": "open",
                    "statusTimestamp": 1_700_000_000_100,
                }
            ],
        }
    )
    fill_events = runtime.process_message(
        {
            "channel": "userFills",
            "data": {
                "isSnapshot": False,
                "fills": [
                    {
                        "coin": "BTC",
                        "side": "B",
                        "px": "60000",
                        "sz": "0.01",
                        "time": 1_700_000_000_100,
                        "oid": 123,
                        "tid": 456,
                        "fee": "0.01",
                        "feeToken": "USDC",
                        "crossed": False,
                        "cloid": "0x11111111111111111111111111111111",
                    }
                ],
            },
        }
    )

    assert runtime.ticker("BTC").bid == Decimal("60000")
    assert order_events[0].event_type == VenueEventType.ORDER_ACCEPTED
    assert order_events[0].order.client_order_id == "business-id"
    assert order_events[0].order.filled_quantity == Decimal("0.01")
    assert fill_events[0].event_type == VenueEventType.FILL
    assert fill_events[0].fill.client_order_id == "business-id"
    assert runtime.process_message({"channel": "userFills", "data": {"fills": [fill_events[0].fill.raw]}}) == ()
