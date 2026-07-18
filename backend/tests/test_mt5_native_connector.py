"""MT5 原生 Connector 与活动订单轮询测试。"""

from decimal import Decimal
from types import SimpleNamespace

from app.venues.domain.events import VenueEventType
from app.venues.domain.models import (
    Fill,
    OrderRequest,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
)
from app.venues.mt5.connector import MT5Connector
from app.venues.mt5.poller import MT5OrderPoller


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_INVALID_FILL = 10030
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    SYMBOL_TRADE_MODE_DISABLED = 0
    ORDER_STATE_STARTED = 0
    ORDER_STATE_PLACED = 1
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_PARTIAL = 3
    ORDER_STATE_FILLED = 4
    ORDER_STATE_EXPIRED = 5
    ORDER_STATE_REJECTED = 6

    def __init__(self):
        self.sent = []

    def last_error(self):
        return (0, "ok")

    def shutdown(self):
        return True

    def terminal_info(self):
        return SimpleNamespace(trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(
            login=123,
            currency="USD",
            equity=10000,
            margin_free=9000,
            margin=1000,
            trade_allowed=True,
            _asdict=lambda: {"login": 123},
        )

    def symbol_select(self, symbol, enabled):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(
            name=symbol,
            currency_base="BTC",
            currency_profit="USD",
            currency_margin="USD",
            volume_step=0.01,
            volume_min=0.01,
            trade_tick_size=0.1,
            point=0.1,
            trade_contract_size=1,
            swap_long=-1.2,
            swap_short=0.4,
            trade_mode=4,
            filling_mode=1,
            _asdict=lambda: {"name": symbol},
        )

    def symbols_get(self):
        return [self.symbol_info("BTCUSD")]

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=60000, ask=60001, time_msc=1_700_000_000_000, volume_real=2)

    def positions_get(self, symbol=None):
        return []

    def orders_get(self, **kwargs):
        return []

    def history_orders_get(self, **kwargs):
        return []

    def history_deals_get(self, **kwargs):
        return [
            SimpleNamespace(
                ticket=55,
                order=44,
                symbol="BTCUSD",
                type=0,
                volume=0.01,
                price=60001,
                commission=-0.1,
                fee=0,
                currency="USD",
                time_msc=1_700_000_000_100,
                _asdict=lambda: {"ticket": 55},
            )
        ]

    def order_send(self, payload):
        self.sent.append(payload)
        return SimpleNamespace(
            retcode=self.TRADE_RETCODE_DONE,
            order=44,
            deal=55,
            volume=payload.get("volume", 0),
            price=payload.get("price", 0),
            comment="ok",
            _asdict=lambda: {"order": 44},
        )

    def market_book_add(self, symbol):
        return False


def test_mt5_connector_maps_account_instrument_and_market_order() -> None:
    mt5 = FakeMT5()
    connector = MT5Connector(
        credentials={"login": "123", "password": "pw", "server": "demo"},
        read_only=False,
        mt5_module=mt5,
        connect=lambda: True,
    )
    try:
        account = connector.get_account()
        instrument = connector.get_instrument("BTCUSD")
        order = connector.submit_order(
            OrderRequest(
                venue="mt5",
                symbol="BTCUSD",
                side=Side.BUY,
                quantity=Decimal("0.01"),
                client_order_id="mt5-native-1",
            )
        )

        assert account.equity == Decimal("10000")
        assert instrument.minimum_quantity == Decimal("0.01")
        assert instrument.long_carry_rate == Decimal("-1.2")
        assert order.status == OrderStatus.FILLED
        assert order.venue_order_id == "44"
        assert mt5.sent[0]["comment"].startswith("CH-")
    finally:
        connector.stop()


def test_mt5_limit_order_uses_pending_action() -> None:
    mt5 = FakeMT5()
    connector = MT5Connector(read_only=False, mt5_module=mt5, connect=lambda: True)
    try:
        connector.submit_order(
            OrderRequest(
                venue="mt5",
                symbol="BTCUSD",
                side=Side.SELL,
                quantity=Decimal("0.01"),
                client_order_id="limit-1",
                order_type=OrderType.LIMIT,
                price=Decimal("61000"),
            )
        )
        assert mt5.sent[0]["action"] == mt5.TRADE_ACTION_PENDING
        assert mt5.sent[0]["type"] == mt5.ORDER_TYPE_SELL_LIMIT
    finally:
        connector.stop()


class PollConnector:
    def __init__(self):
        self.calls = 0

    def get_order(self, symbol, **kwargs):
        self.calls += 1
        return OrderSnapshot(
            venue="mt5",
            symbol=symbol,
            client_order_id=kwargs["client_order_id"],
            venue_order_id=kwargs["venue_order_id"],
            status=OrderStatus.FILLED,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            requested_quantity=Decimal("1"),
            filled_quantity=Decimal("1"),
            remaining_quantity=Decimal("0"),
        )

    def get_fills(self, symbol, **kwargs):
        return [
            Fill(
                venue="mt5",
                symbol=symbol,
                trade_id="deal-1",
                client_order_id=kwargs["client_order_id"],
                venue_order_id=kwargs["venue_order_id"],
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("100"),
            )
        ]


def test_mt5_active_order_poller_emits_once_and_stops_watching_terminal_order() -> None:
    connector = PollConnector()
    poller = MT5OrderPoller(connector)
    events = []
    poller._handlers.append(events.append)
    poller.start = lambda: None
    poller.watch("BTCUSD", "44", "cid")

    assert poller.poll_once() == 2
    assert poller.poll_once() == 0
    assert [event.event_type for event in events] == [VenueEventType.ORDER_FILLED, VenueEventType.FILL]
