"""独立 Paper Connector。

Paper 使用真实行情缓存或注入的订单簿进行本地撮合，但绝不会调用真实交易接口。
所有结果仍转换为统一订单与成交事件，因此上层生命周期与 Live 完全一致。
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from decimal import Decimal
from threading import RLock

from app.core.time_utils import utc_now
from app.venues.domain.capabilities import VenueCapabilities
from app.venues.domain.events import VenueEvent, VenueEventType
from app.venues.domain.models import (
    AccountSnapshot,
    CredentialCheck,
    CredentialCheckItem,
    Fill,
    Instrument,
    OrderBookSnapshot,
    OrderRequest,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Position,
    Ticker,
)
from app.venues.protocols import EventHandler


class PaperConnector:
    """内存 Paper 账本和撮合器。"""

    capabilities = VenueCapabilities(
        venue="paper",
        supports_post_only=True,
        supports_reduce_only=True,
        supports_hedge_mode=True,
        supports_native_order_events=True,
    )

    def __init__(
        self,
        *,
        venue: str,
        ticker_provider=None,
        book_provider=None,
        initial_equity: Decimal = Decimal("50000"),
        maker_fee_rate: Decimal = Decimal("0.00015"),
        taker_fee_rate: Decimal = Decimal("0.00045"),
        latency_ms: int = 0,
    ) -> None:
        self.venue = str(venue).strip().lower()
        self._ticker_provider = ticker_provider
        self._book_provider = book_provider
        self._initial_equity = initial_equity
        self._maker_fee_rate = maker_fee_rate
        self._taker_fee_rate = taker_fee_rate
        self._latency_ms = max(int(latency_ms), 0)
        self._orders: dict[str, OrderSnapshot] = {}
        self._fills: dict[str, Fill] = {}
        self._handlers: list[EventHandler] = []
        self._lock = RLock()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def health(self) -> dict:
        return {"venue": self.venue, "status": "ok", "mode": "paper"}

    def get_account(self) -> AccountSnapshot:
        fees = sum((fill.commission for fill in self._fills.values()), Decimal("0"))
        equity = self._initial_equity - fees
        return AccountSnapshot(
            venue=self.venue,
            account_id=f"paper:{self.venue}",
            currency="USD",
            equity=equity,
            available_balance=equity,
            margin_used=Decimal("0"),
        )

    def get_positions(self) -> list[Position]:
        return []

    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]:
        active = {OrderStatus.SUBMITTED, OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}
        return [
            order for order in self._orders.values()
            if order.status in active and (symbol is None or order.symbol == symbol)
        ]

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]:
        values = list(symbols or ("BTC", "ETH", "SOL"))
        return [self.get_instrument(symbol) for symbol in values]

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        return Instrument(
            venue=self.venue,
            symbol=symbol,
            base_asset=symbol.replace("USDT", "").replace("USD", ""),
            quote_asset="USD",
            settlement_asset="USD",
            quantity_step=Decimal("0.000001"),
            minimum_quantity=Decimal("0.000001"),
            price_tick=Decimal("0.01"),
            maker_fee_rate=self._maker_fee_rate,
            taker_fee_rate=self._taker_fee_rate,
        )

    def get_ticker(self, symbol: str) -> Ticker:
        if self._ticker_provider is not None:
            try:
                return self._ticker_provider(symbol)
            except LookupError:
                pass
        base = {"BTC": Decimal("65000"), "ETH": Decimal("3400"), "SOL": Decimal("145")}.get(
            symbol.upper().replace("USDT", "").replace("USD", ""), Decimal("100")
        )
        spread = base * Decimal("0.0004")
        return Ticker(self.venue, symbol, base - spread / 2, base + spread / 2)

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        if self._book_provider is not None:
            return self._book_provider(symbol, depth)
        ticker = self.get_ticker(symbol)
        quantity = Decimal("100000") / max(ticker.ask, Decimal("1"))
        return OrderBookSnapshot(
            self.venue,
            symbol,
            ((ticker.bid, quantity),),
            ((ticker.ask, quantity),),
        )

    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None:
        return None

    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None:
        return None

    def subscribe_private_events(self, handler: EventHandler) -> None:
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        if self._latency_ms:
            time.sleep(self._latency_ms / 1000)
        with self._lock:
            existing = self._orders.get(request.client_order_id)
            if existing is not None:
                return existing
            order = self._match(request)
            self._orders[request.client_order_id] = order
            self._emit_order(order)
            if order.filled_quantity > 0:
                self._record_fill(order, request)
            return order

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        with self._lock:
            order = self.get_order(symbol, client_order_id=client_order_id, venue_order_id=venue_order_id)
            if order.status in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED}:
                return order
            canceled = OrderSnapshot(**{**order.__dict__, "status": OrderStatus.CANCELED, "updated_at": utc_now()})
            self._orders[canceled.client_order_id] = canceled
            self._emit_order(canceled)
            return canceled

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if client_order_id and client_order_id in self._orders:
            return self._orders[client_order_id]
        for order in self._orders.values():
            if order.venue_order_id == venue_order_id and order.symbol == symbol:
                return order
        raise LookupError(f"Paper 订单不存在: {client_order_id or venue_order_id}")

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        return [
            fill for fill in self._fills.values()
            if (symbol is None or fill.symbol == symbol)
            and (not client_order_id or fill.client_order_id == client_order_id)
            and (not venue_order_id or fill.venue_order_id == venue_order_id)
        ]

    def validate_credentials(self) -> CredentialCheck:
        item = CredentialCheckItem("paper", True, "Paper 模式不需要凭据", blocking=False)
        return CredentialCheck(self.venue, "paper", f"paper:{self.venue}", True, True, True, (item,))

    def _match(self, request: OrderRequest) -> OrderSnapshot:
        book = self.get_order_book(request.symbol)
        best_bid = book.bids[0][0] if book.bids else Decimal("0")
        best_ask = book.asks[0][0] if book.asks else Decimal("0")
        venue_order_id = f"paper-{self.venue}-{len(self._orders) + 1}"

        if request.order_type == OrderType.LIMIT and request.post_only:
            marketable = (
                request.side.value == "buy" and request.price is not None and request.price >= best_ask
            ) or (
                request.side.value == "sell" and request.price is not None and request.price <= best_bid
            )
            if marketable:
                return self._snapshot(request, venue_order_id, OrderStatus.REJECTED)
            return self._snapshot(request, venue_order_id, OrderStatus.ACCEPTED)

        levels = book.asks if request.side.value == "buy" else book.bids
        remaining = request.quantity
        filled = Decimal("0")
        notional = Decimal("0")
        for price, available in levels:
            take = min(remaining, available)
            filled += take
            notional += take * price
            remaining -= take
            if remaining <= 0:
                break
        average = notional / filled if filled > 0 else None
        status = OrderStatus.FILLED if remaining <= 0 else OrderStatus.PARTIALLY_FILLED if filled > 0 else OrderStatus.REJECTED
        fee_rate = self._maker_fee_rate if request.post_only else self._taker_fee_rate
        return self._snapshot(request, venue_order_id, status, filled, average, notional * fee_rate)

    def _snapshot(
        self,
        request: OrderRequest,
        venue_order_id: str,
        status: OrderStatus,
        filled: Decimal = Decimal("0"),
        average: Decimal | None = None,
        commission: Decimal = Decimal("0"),
    ) -> OrderSnapshot:
        return OrderSnapshot(
            venue=self.venue,
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            venue_order_id=venue_order_id,
            status=status,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.quantity,
            filled_quantity=filled,
            remaining_quantity=max(request.quantity - filled, Decimal("0")),
            average_price=average,
            price=request.price,
            commission=commission,
            position_side=request.position_side,
        )

    def _record_fill(self, order: OrderSnapshot, request: OrderRequest) -> None:
        digest = hashlib.sha256(f"{order.venue_order_id}:{order.filled_quantity}:{order.average_price}".encode()).hexdigest()[:24]
        fill = Fill(
            venue=self.venue,
            symbol=order.symbol,
            trade_id=f"paper-{digest}",
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
            side=order.side,
            quantity=order.filled_quantity,
            price=order.average_price or Decimal("0"),
            commission=order.commission,
            is_maker=request.post_only,
        )
        self._fills[fill.trade_id] = fill
        self._emit(VenueEvent(fill.trade_id, self.venue, VenueEventType.FILL, fill.occurred_at, fill=fill))

    def _emit_order(self, order: OrderSnapshot) -> None:
        event_type = {
            OrderStatus.ACCEPTED: VenueEventType.ORDER_ACCEPTED,
            OrderStatus.PARTIALLY_FILLED: VenueEventType.ORDER_PARTIALLY_FILLED,
            OrderStatus.FILLED: VenueEventType.ORDER_FILLED,
            OrderStatus.CANCELED: VenueEventType.ORDER_CANCELED,
            OrderStatus.REJECTED: VenueEventType.ORDER_REJECTED,
        }.get(order.status, VenueEventType.ORDER_SUBMITTED)
        event_id = f"paper:{order.client_order_id}:{order.status}:{order.filled_quantity}"
        self._emit(VenueEvent(event_id, self.venue, event_type, order.updated_at, order=order))

    def _emit(self, event: VenueEvent) -> None:
        for handler in tuple(self._handlers):
            handler(event)
