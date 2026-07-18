"""原生交易场所领域层测试。"""

from decimal import Decimal

import pytest

from app.venues.domain.events import VenueEventType
from app.venues.domain.models import OrderRequest, OrderStatus, OrderType, Side
from app.venues.paper import PaperConnector
from app.venues.registry import VenueRegistry


def test_registry_requires_explicit_native_connector() -> None:
    registry = VenueRegistry()
    registry.register("BINANCE", lambda **_: object())

    assert registry.venues() == ("binance",)
    assert registry.create("Binance") is not None
    with pytest.raises(ValueError, match="尚未接入"):
        registry.create("okx")


def test_paper_market_order_emits_order_and_fill_events() -> None:
    connector = PaperConnector(venue="binance", latency_ms=0)
    events = []
    connector.subscribe_private_events(events.append)

    order = connector.submit_order(
        OrderRequest(
            venue="binance",
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            client_order_id="paper-market-1",
        )
    )

    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == Decimal("0.01")
    assert [event.event_type for event in events] == [VenueEventType.ORDER_FILLED, VenueEventType.FILL]
    assert len(connector.get_fills(client_order_id="paper-market-1")) == 1


def test_paper_client_order_id_is_idempotent() -> None:
    connector = PaperConnector(venue="hyperliquid")
    request = OrderRequest(
        venue="hyperliquid",
        symbol="BTC",
        side=Side.SELL,
        quantity=Decimal("0.02"),
        client_order_id="stable-id",
    )

    first = connector.submit_order(request)
    second = connector.submit_order(request)

    assert second == first
    assert len(connector.get_fills(client_order_id="stable-id")) == 1


def test_paper_post_only_marketable_order_is_rejected() -> None:
    connector = PaperConnector(venue="hyperliquid")
    ticker = connector.get_ticker("BTC")

    order = connector.submit_order(
        OrderRequest(
            venue="hyperliquid",
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            client_order_id="post-only-cross",
            order_type=OrderType.LIMIT,
            price=ticker.ask,
            post_only=True,
        )
    )

    assert order.status == OrderStatus.REJECTED
    assert order.filled_quantity == 0


def test_paper_resting_maker_can_be_canceled() -> None:
    connector = PaperConnector(venue="binance")
    ticker = connector.get_ticker("BTCUSDT")
    accepted = connector.submit_order(
        OrderRequest(
            venue="binance",
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            client_order_id="resting-maker",
            order_type=OrderType.LIMIT,
            price=ticker.bid - Decimal("1"),
            post_only=True,
        )
    )

    canceled = connector.cancel_order("BTCUSDT", client_order_id=accepted.client_order_id)

    assert accepted.status == OrderStatus.ACCEPTED
    assert canceled.status == OrderStatus.CANCELED
