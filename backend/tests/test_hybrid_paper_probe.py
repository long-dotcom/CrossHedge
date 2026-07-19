"""混合 Paper 探针：真实最小数量、Maker 部分成交、立即回平和策略数量投影。"""

from decimal import Decimal

import fakeredis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ProbeRun, SystemSetting
from app.venues.domain.models import (
    AccountSnapshot,
    Instrument,
    OrderRequest,
    OrderBookSnapshot,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    Ticker,
)
from app.venues.hybrid_probe import HybridPaperProbeConnector


class FakeLiveConnector:
    venue = "hyperliquid"
    environment = "live"
    read_only = False
    capabilities = object()

    def __init__(self, *, maker_partial: bool = False) -> None:
        self.maker_partial = maker_partial
        self.position = Decimal("0")
        self.orders: dict[str, OrderSnapshot] = {}
        self.submitted: list[OrderRequest] = []

    def subscribe_private_events(self, handler):
        self.private_handler = handler

    def get_account(self):
        return AccountSnapshot("hyperliquid", "probe-account", "USDC", Decimal("100"), Decimal("100"), Decimal("0"))

    def get_positions(self):
        if self.position <= 0:
            return []
        return [Position(
            "hyperliquid", "probe-account", "BTC", PositionSide.NET,
            self.position, Decimal("100"), Decimal("100"),
        )]

    def get_instrument(self, symbol, refresh=False):
        return Instrument(
            "hyperliquid", symbol, "BTC", "USDC", "USDC",
            Decimal("0.001"), Decimal("0.001"), Decimal("0.1"),
            minimum_notional=Decimal("10"),
        )

    def get_ticker(self, symbol):
        return Ticker("hyperliquid", symbol, Decimal("99"), Decimal("100"))

    def get_order_book(self, symbol, depth=20):
        return OrderBookSnapshot(
            "hyperliquid", symbol,
            ((Decimal("99"), Decimal("10")),),
            ((Decimal("100"), Decimal("10")),),
        )

    def submit_order(self, request: OrderRequest):
        self.submitted.append(request)
        if request.client_order_id.endswith("-F"):
            self.position = max(self.position - request.quantity, Decimal("0"))
            result = _snapshot(request, OrderStatus.FILLED, request.quantity, Decimal("100.5"))
        elif request.order_type == OrderType.LIMIT and self.maker_partial:
            filled = request.quantity / Decimal("2")
            self.position += filled
            result = _snapshot(request, OrderStatus.ACCEPTED, Decimal("0"), None)
            self.orders[request.client_order_id] = _snapshot(request, OrderStatus.PARTIALLY_FILLED, filled, request.price)
            return result
        else:
            self.position += request.quantity
            result = _snapshot(request, OrderStatus.FILLED, request.quantity, Decimal("100"))
        self.orders[request.client_order_id] = result
        return result

    def get_order(self, symbol, *, client_order_id="", venue_order_id=""):
        if client_order_id not in self.orders:
            raise LookupError(client_order_id)
        return self.orders[client_order_id]

    def cancel_order(self, symbol, *, client_order_id="", venue_order_id=""):
        current = self.orders[client_order_id]
        canceled = OrderSnapshot(**{**current.__dict__, "status": OrderStatus.CANCELED})
        self.orders[client_order_id] = canceled
        return canceled

    def get_fills(self, *args, **kwargs):
        return []


def _snapshot(request, status, filled, average):
    return OrderSnapshot(
        venue=request.venue,
        symbol=request.symbol,
        client_order_id=request.client_order_id,
        venue_order_id=f"venue-{request.client_order_id}",
        status=status,
        side=request.side,
        order_type=request.order_type,
        requested_quantity=request.quantity,
        filled_quantity=filled,
        remaining_quantity=max(request.quantity - filled, Decimal("0")),
        average_price=average,
        price=request.price,
        position_side=request.position_side,
    )


def _factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _request(order_type=OrderType.MARKET):
    return OrderRequest(
        venue="hyperliquid",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        client_order_id="CH-10-20",
        order_type=order_type,
        price=Decimal("99") if order_type == OrderType.LIMIT else None,
        post_only=order_type == OrderType.LIMIT,
        metadata={"intent_id": 10, "hedge_group_id": 1, "action": "OPEN"},
    )


def test_market_probe_uses_minimum_real_quantity_and_projects_strategy_quantity(monkeypatch) -> None:
    factory = _factory()
    monkeypatch.setattr("app.venues.hybrid_probe.SessionLocal", factory)
    monkeypatch.setattr("app.venues.hybrid_probe.paper_live_probe_enabled_for_venue", lambda *args: True)
    live = FakeLiveConnector()
    connector = HybridPaperProbeConnector(live, redis=fakeredis.FakeRedis(decode_responses=True))

    result = connector.submit_order(_request())

    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == Decimal("2")
    assert result.average_price == Decimal("100")
    assert [order.quantity for order in live.submitted] == [Decimal("0.1"), Decimal("0.1")]
    assert live.position == 0
    with factory() as db:
        run = db.query(ProbeRun).one()
        assert run.status == "FLAT"
        assert run.probe_quantity == 0.1
        assert run.residual_quantity == 0


def test_maker_partial_fill_is_sampled_then_flattened(monkeypatch) -> None:
    factory = _factory()
    with factory() as db:
        db.add(SystemSetting(key="paper_probe_maker_timeout_seconds", value="0.01"))
        db.commit()
    monkeypatch.setattr("app.venues.hybrid_probe.SessionLocal", factory)
    monkeypatch.setattr("app.venues.hybrid_probe.paper_live_probe_enabled_for_venue", lambda *args: True)
    live = FakeLiveConnector(maker_partial=True)
    connector = HybridPaperProbeConnector(live, redis=fakeredis.FakeRedis(decode_responses=True))

    result = connector.submit_order(_request(OrderType.LIMIT))

    assert result.status == OrderStatus.FILLED
    assert result.filled_quantity == Decimal("2")
    assert live.submitted[0].post_only is True
    assert live.submitted[1].order_type == OrderType.MARKET
    assert live.submitted[1].quantity == Decimal("0.051")
    assert live.position == 0
