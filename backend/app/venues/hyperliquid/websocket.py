"""Hyperliquid 公共和用户事件 WebSocket runtime。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.venues.domain.events import VenueEvent, VenueEventType
from app.venues.domain.models import (
    Fill,
    OrderBookSnapshot,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    Ticker,
)
from app.venues.protocols import EventHandler

logger = get_logger(__name__)


class HyperliquidWebSocketRuntime:
    def __init__(
        self,
        *,
        ws_url: str,
        account_address: str = "",
        on_ticker: Callable[[Ticker], None] | None = None,
        on_book: Callable[[OrderBookSnapshot], None] | None = None,
    ) -> None:
        self.ws_url = ws_url
        self.account_address = account_address
        self._on_ticker = on_ticker
        self._on_book = on_book
        self._handlers: list[EventHandler] = []
        self._private_enabled = False
        self._symbols: set[str] = set()
        self._books: dict[str, OrderBookSnapshot] = {}
        self._tickers: dict[str, Ticker] = {}
        self._cloid_to_client: dict[str, str] = {}
        self._seen_fills: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._last_message_at = 0.0

    def register_client_order_id(self, venue_cloid: str, client_order_id: str) -> None:
        self._cloid_to_client[venue_cloid.lower()] = client_order_id

    def add_symbols(self, symbols: Sequence[str]) -> None:
        self._symbols.update(str(symbol) for symbol in symbols)
        self.start()

    def remove_symbols(self, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            self._symbols.discard(str(symbol))

    def add_event_handler(self, handler: EventHandler, *, private: bool = False) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)
        self._private_enabled = self._private_enabled or private
        self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hyperliquid-native-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def health(self) -> dict[str, Any]:
        return {
            "ws_running": bool(self._thread and self._thread.is_alive()),
            "ws_connected": self._connected,
            "message_age_seconds": time.monotonic() - self._last_message_at if self._last_message_at else None,
            "symbols": sorted(self._symbols),
        }

    def ticker(self, symbol: str) -> Ticker | None:
        return self._tickers.get(symbol)

    def order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot | None:
        book = self._books.get(symbol)
        if book is None:
            return None
        return OrderBookSnapshot(
            **{**book.__dict__, "bids": book.bids[:depth], "asks": book.asks[:depth]}
        )

    def process_message(self, payload: dict[str, Any]) -> tuple[VenueEvent, ...]:
        channel = str(payload.get("channel") or "")
        data = payload.get("data") or {}
        events: list[VenueEvent] = []
        if channel == "l2Book" and isinstance(data, dict):
            self._process_book(data)
        elif channel == "orderUpdates":
            for item in data if isinstance(data, list) else []:
                event = self._order_event(item)
                if event:
                    events.append(event)
        elif channel == "userFills" and isinstance(data, dict):
            for item in data.get("fills", []):
                event = self._fill_event(item)
                if event:
                    events.append(event)
        elif channel == "user" and isinstance(data, dict) and "fills" in data:
            for item in data.get("fills", []):
                event = self._fill_event(item)
                if event:
                    events.append(event)
        for event in events:
            for handler in tuple(self._handlers):
                handler(event)
        return tuple(events)

    def _process_book(self, data: dict[str, Any]) -> None:
        symbol = str(data.get("coin") or "")
        levels = data.get("levels") or [[], []]
        bids = tuple((_decimal(row.get("px")), _decimal(row.get("sz"))) for row in levels[0])
        asks = tuple((_decimal(row.get("px")), _decimal(row.get("sz"))) for row in levels[1])
        exchange_time = _millis_datetime(data.get("time"))
        book = OrderBookSnapshot("hyperliquid", symbol, bids, asks, exchange_time=exchange_time)
        self._books[symbol] = book
        if bids and asks:
            ticker = Ticker(
                "hyperliquid",
                symbol,
                bids[0][0],
                asks[0][0],
                bids[0][1],
                asks[0][1],
                exchange_time=exchange_time,
            )
            self._tickers[symbol] = ticker
            if self._on_ticker:
                self._on_ticker(ticker)
        if self._on_book:
            self._on_book(book)

    def _order_event(self, item: dict[str, Any]) -> VenueEvent | None:
        raw_order = item.get("order") or {}
        status = _order_status(item.get("status"))
        if not raw_order:
            return None
        cloid = str(raw_order.get("cloid") or "").lower()
        client_order_id = self._cloid_to_client.get(cloid, cloid)
        requested = _decimal(raw_order.get("origSz") or raw_order.get("sz"))
        remaining = _decimal(raw_order.get("sz"))
        filled = max(requested - remaining, Decimal("0"))
        order = OrderSnapshot(
            venue="hyperliquid",
            symbol=str(raw_order.get("coin") or ""),
            client_order_id=client_order_id,
            venue_order_id=str(raw_order.get("oid") or ""),
            status=status,
            side=Side.BUY if str(raw_order.get("side") or "B") == "B" else Side.SELL,
            order_type=OrderType.LIMIT,
            requested_quantity=requested,
            filled_quantity=filled,
            remaining_quantity=remaining,
            price=_optional_decimal(raw_order.get("limitPx")),
            position_side=PositionSide.NET,
            updated_at=_millis_datetime(item.get("statusTimestamp") or raw_order.get("timestamp")) or utc_now(),
            raw=item,
        )
        return VenueEvent(
            _event_id("order", order.venue_order_id, status.value, str(item.get("statusTimestamp") or "")),
            "hyperliquid",
            _order_event_type(status),
            order.updated_at,
            order=order,
            raw=item,
        )

    def _fill_event(self, item: dict[str, Any]) -> VenueEvent | None:
        trade_id = str(item.get("tid") or "")
        symbol = str(item.get("coin") or "")
        unique_id = f"{item.get('time', '')}:{symbol}:{trade_id}"
        if unique_id in self._seen_fills:
            return None
        self._seen_fills.add(unique_id)
        cloid = str(item.get("cloid") or "").lower()
        fill = Fill(
            venue="hyperliquid",
            symbol=symbol,
            trade_id=trade_id,
            client_order_id=self._cloid_to_client.get(cloid, cloid),
            venue_order_id=str(item.get("oid") or ""),
            side=Side.BUY if str(item.get("side") or "B") == "B" else Side.SELL,
            quantity=_decimal(item.get("sz")),
            price=_decimal(item.get("px")),
            commission=_decimal(item.get("fee")),
            commission_asset=str(item.get("feeToken") or "USDC"),
            is_maker=not bool(item.get("crossed")),
            occurred_at=_millis_datetime(item.get("time")) or utc_now(),
            raw=item,
        )
        return VenueEvent(
            f"hyperliquid:fill:{unique_id}",
            "hyperliquid",
            VenueEventType.FILL,
            fill.occurred_at,
            fill=fill,
            raw=item,
        )

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            subscriptions = [{"type": "l2Book", "coin": symbol} for symbol in sorted(self._symbols)]
            if self.account_address and self._private_enabled:
                subscriptions.extend(
                    (
                        {"type": "orderUpdates", "user": self.account_address},
                        {"type": "userFills", "user": self.account_address},
                    )
                )
            if not subscriptions:
                await asyncio.sleep(0.25)
                continue
            try:
                async with websockets.connect(self.ws_url, ping_interval=None, open_timeout=10) as ws:
                    for subscription in subscriptions:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": subscription}))
                    self._connected = True
                    backoff = 1.0
                    last_ping = time.monotonic()
                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            self._last_message_at = time.monotonic()
                            self.process_message(json.loads(raw))
                        except asyncio.TimeoutError:
                            if time.monotonic() - last_ping >= 30:
                                await ws.send(json.dumps({"method": "ping"}))
                                last_ping = time.monotonic()
            except Exception as exc:
                logger.warning("Hyperliquid 原生 WS 断开: {}", exc)
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _order_status(value: Any) -> OrderStatus:
    status = str(value or "").lower()
    if status in {"open", "triggered"}:
        return OrderStatus.ACCEPTED
    if status == "filled":
        return OrderStatus.FILLED
    if status.startswith("canceled") or status.startswith("cancelled"):
        return OrderStatus.CANCELED
    if status in {"rejected", "margincanceled", "siblingfilledcanceled"}:
        return OrderStatus.REJECTED
    if "canceled" in status or "cancelled" in status:
        return OrderStatus.CANCELED
    return OrderStatus.UNKNOWN


def _order_event_type(status: OrderStatus) -> VenueEventType:
    return {
        OrderStatus.ACCEPTED: VenueEventType.ORDER_ACCEPTED,
        OrderStatus.PARTIALLY_FILLED: VenueEventType.ORDER_PARTIALLY_FILLED,
        OrderStatus.FILLED: VenueEventType.ORDER_FILLED,
        OrderStatus.CANCELED: VenueEventType.ORDER_CANCELED,
        OrderStatus.REJECTED: VenueEventType.ORDER_REJECTED,
    }.get(status, VenueEventType.ORDER_UNKNOWN)


def _event_id(*parts: str) -> str:
    basis = ":".join(parts)
    return f"hyperliquid:{hashlib.sha256(basis.encode()).hexdigest()[:40]}"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def _millis_datetime(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)
