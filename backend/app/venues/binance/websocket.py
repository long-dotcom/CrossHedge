"""Binance Futures 公私有 WebSocket runtime。"""

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
from app.venues.binance.orderbook import BinanceLocalOrderBook, OrderBookGap
from app.venues.binance.rest import BinanceFuturesRestClient, normalize_symbol
from app.venues.domain.events import VenueEvent, VenueEventType
from app.venues.domain.models import (
    Fill,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    Ticker,
)
from app.venues.protocols import EventHandler

logger = get_logger(__name__)

PUBLIC_WS_URLS = {
    "live": "wss://fstream.binance.com/public/ws",
    "testnet": "wss://stream.binancefuture.com/ws",
    "demo": "wss://fstream.binance.com/public/ws",
}
PRIVATE_WS_URLS = {
    "live": "wss://fstream.binance.com/private/ws",
    "testnet": "wss://stream.binancefuture.com/ws",
    "demo": "wss://fstream.binance.com/private/ws",
}


class BinanceWebSocketRuntime:
    """一个 Connector 对应一组可恢复的公共/私有 WS 循环。"""

    def __init__(
        self,
        rest: BinanceFuturesRestClient,
        *,
        on_ticker: Callable[[Ticker], None] | None = None,
        on_book: Callable[[Any], None] | None = None,
    ) -> None:
        self.rest = rest
        self.environment = str(getattr(rest, "environment", "live") or "live")
        self._on_ticker = on_ticker
        self._on_book = on_book
        self._handlers: list[EventHandler] = []
        self._private_enabled = False
        self._symbols: set[str] = set()
        self._books: dict[str, BinanceLocalOrderBook] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._market_connected = False
        self._private_connected = False
        self._last_market_message_at = 0.0
        self._last_private_message_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="binance-native-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def add_symbols(self, symbols: Sequence[str]) -> None:
        self._symbols.update(normalize_symbol(symbol) for symbol in symbols)
        self.start()

    def remove_symbols(self, symbols: Sequence[str]) -> None:
        for symbol in symbols:
            normalized = normalize_symbol(symbol)
            self._symbols.discard(normalized)
            self._books.pop(normalized, None)

    def add_event_handler(self, handler: EventHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)
        self._private_enabled = True
        self.start()

    def health(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "market_ws_connected": self._market_connected,
            "private_ws_connected": self._private_connected,
            "market_message_age_seconds": now - self._last_market_message_at if self._last_market_message_at else None,
            "private_message_age_seconds": now - self._last_private_message_at if self._last_private_message_at else None,
            "symbols": sorted(self._symbols),
            "order_books_synchronized": {
                symbol: book.synchronized for symbol, book in self._books.items()
            },
        }

    def order_book(self, symbol: str, depth: int = 20):
        book = self._books.get(normalize_symbol(symbol))
        return book.snapshot(depth=depth) if book else None

    def process_market_message(self, payload: dict[str, Any]) -> None:
        """公开给测试和 runtime 的确定性消息处理入口。"""
        data = payload.get("data") if "data" in payload else payload
        if not isinstance(data, dict):
            return
        event_type = str(data.get("e") or "")
        if event_type == "bookTicker" or {"b", "a", "B", "A", "s"}.issubset(data):
            ticker = Ticker(
                venue="binance",
                symbol=str(data.get("s") or ""),
                bid=_decimal(data.get("b")),
                ask=_decimal(data.get("a")),
                bid_quantity=_decimal(data.get("B")),
                ask_quantity=_decimal(data.get("A")),
                exchange_time=_millis_datetime(data.get("E") or data.get("T")),
            )
            if self._on_ticker:
                self._on_ticker(ticker)
            return
        if event_type != "depthUpdate":
            return
        symbol = normalize_symbol(data.get("s"))
        book = self._books.setdefault(symbol, BinanceLocalOrderBook(symbol))
        if book.last_update_id is None:
            book.initialize(self.rest.depth(symbol, 1000))
        try:
            changed = book.apply(data)
        except OrderBookGap:
            book.initialize(self.rest.depth(symbol, 1000))
            try:
                changed = book.apply(data)
            except OrderBookGap:
                logger.warning("Binance 订单簿重新同步时当前增量仍不匹配: {}", symbol)
                return
        if changed and self._on_book:
            self._on_book(book.snapshot(depth=20, exchange_time=_millis_datetime(data.get("E"))))

    def process_private_message(self, payload: dict[str, Any]) -> tuple[VenueEvent, ...]:
        """把 Binance 用户流消息转换成统一事件。"""
        event_name = str(payload.get("e") or "")
        events: list[VenueEvent] = []
        if event_name == "ORDER_TRADE_UPDATE":
            order_data = payload.get("o") or {}
            order = _order_from_update(order_data, payload)
            events.append(
                VenueEvent(
                    _event_id(payload, order.client_order_id, order.status.value),
                    "binance",
                    _order_event_type(order.status),
                    _millis_datetime(payload.get("T") or payload.get("E")) or utc_now(),
                    order=order,
                    raw=payload,
                )
            )
            last_quantity = _decimal(order_data.get("l"))
            trade_id = str(order_data.get("t") or "")
            if last_quantity > 0 and trade_id not in {"", "0", "-1"}:
                fill = _fill_from_order_update(order_data, payload, order)
                events.append(
                    VenueEvent(
                        f"binance:trade:{fill.symbol}:{fill.trade_id}",
                        "binance",
                        VenueEventType.FILL,
                        fill.occurred_at,
                        fill=fill,
                        raw=payload,
                    )
                )
        elif event_name == "TRADE_LITE":
            fill = _fill_from_trade_lite(payload)
            events.append(
                VenueEvent(
                    f"binance:trade:{fill.symbol}:{fill.trade_id}",
                    "binance",
                    VenueEventType.FILL,
                    fill.occurred_at,
                    fill=fill,
                    raw=payload,
                )
            )
        elif event_name == "listenKeyExpired":
            events.append(
                VenueEvent(
                    _event_id(payload, "stream", "expired"),
                    "binance",
                    VenueEventType.RECONCILIATION_REQUIRED,
                    _millis_datetime(payload.get("E")) or utc_now(),
                    raw=payload,
                )
            )
        for event in events:
            for handler in tuple(self._handlers):
                handler(event)
        return tuple(events)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._loop = None

    async def _main(self) -> None:
        tasks = [asyncio.create_task(self._market_loop())]
        if self.rest.api_key and self._private_enabled:
            tasks.append(asyncio.create_task(self._private_loop()))
        await asyncio.gather(*tasks)

    async def _market_loop(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            if not self._symbols:
                await asyncio.sleep(0.25)
                continue
            streams = []
            for symbol in sorted(self._symbols):
                streams.extend((f"{symbol.lower()}@bookTicker", f"{symbol.lower()}@depth@100ms"))
            url = PUBLIC_WS_URLS[self.environment]
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))
                    self._market_connected = True
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        self._last_market_message_at = time.monotonic()
                        self.process_market_message(json.loads(raw))
            except asyncio.TimeoutError:
                logger.warning("Binance 公共 WS 30 秒无消息，准备重连")
            except Exception as exc:
                logger.warning("Binance 公共 WS 断开: {}", exc)
            finally:
                self._market_connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _private_loop(self) -> None:
        import websockets

        backoff = 1.0
        while not self._stop.is_set():
            listen_key = ""
            try:
                listen_key = await asyncio.to_thread(self.rest.create_listen_key)
                url = f"{PRIVATE_WS_URLS[self.environment]}/{listen_key}"
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    self._private_connected = True
                    backoff = 1.0
                    next_keepalive = time.monotonic() + 30 * 60
                    while not self._stop.is_set():
                        timeout = max(min(next_keepalive - time.monotonic(), 30), 1)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                            self._last_private_message_at = time.monotonic()
                            events = self.process_private_message(json.loads(raw))
                            if any(event.event_type == VenueEventType.RECONCILIATION_REQUIRED for event in events):
                                break
                        except asyncio.TimeoutError:
                            pass
                        if time.monotonic() >= next_keepalive:
                            await asyncio.to_thread(self.rest.keepalive_listen_key, listen_key)
                            next_keepalive = time.monotonic() + 30 * 60
            except Exception as exc:
                logger.warning("Binance 私有 WS 断开: {}", exc)
            finally:
                self._private_connected = False
                if listen_key:
                    try:
                        await asyncio.to_thread(self.rest.close_listen_key, listen_key)
                    except Exception:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def _order_from_update(data: dict[str, Any], envelope: dict[str, Any]) -> OrderSnapshot:
    requested = _decimal(data.get("q"))
    filled = _decimal(data.get("z"))
    raw_position_side = str(data.get("ps") or "BOTH")
    order_type = str(data.get("o") or "MARKET")
    return OrderSnapshot(
        venue="binance",
        symbol=str(data.get("s") or ""),
        client_order_id=str(data.get("c") or ""),
        venue_order_id=str(data.get("i") or ""),
        status=_status(data.get("X")),
        side=Side(str(data.get("S") or "BUY").lower()),
        order_type=OrderType.LIMIT if order_type == "LIMIT" else OrderType.MARKET,
        requested_quantity=requested,
        filled_quantity=filled,
        remaining_quantity=max(requested - filled, Decimal("0")),
        average_price=_positive_decimal(data.get("ap")),
        price=_positive_decimal(data.get("p")),
        # 手续费只由独立 Fill 事件累计，避免订单事件与 Fill 双计。
        commission=Decimal("0"),
        position_side=PositionSide.NET if raw_position_side == "BOTH" else PositionSide(raw_position_side),
        updated_at=_millis_datetime(envelope.get("T") or envelope.get("E")) or utc_now(),
        raw=data,
    )


def _fill_from_order_update(
    data: dict[str, Any], envelope: dict[str, Any], order: OrderSnapshot
) -> Fill:
    return Fill(
        venue="binance",
        symbol=order.symbol,
        trade_id=str(data.get("t") or ""),
        client_order_id=order.client_order_id,
        venue_order_id=order.venue_order_id,
        side=order.side,
        quantity=_decimal(data.get("l")),
        price=_decimal(data.get("L")),
        commission=_decimal(data.get("n")),
        commission_asset=str(data.get("N") or ""),
        is_maker=bool(data.get("m")),
        occurred_at=_millis_datetime(envelope.get("T") or envelope.get("E")) or utc_now(),
        raw=data,
    )


def _fill_from_trade_lite(data: dict[str, Any]) -> Fill:
    return Fill(
        venue="binance",
        symbol=str(data.get("s") or ""),
        trade_id=str(data.get("t") or ""),
        client_order_id=str(data.get("c") or ""),
        venue_order_id=str(data.get("i") or ""),
        side=Side(str(data.get("S") or "BUY").lower()),
        quantity=_decimal(data.get("q")),
        price=_decimal(data.get("p")),
        is_maker=bool(data.get("m")),
        occurred_at=_millis_datetime(data.get("T") or data.get("E")) or utc_now(),
        raw=data,
    )


def _status(value: Any) -> OrderStatus:
    return {
        "NEW": OrderStatus.ACCEPTED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "EXPIRED": OrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
        "REJECTED": OrderStatus.REJECTED,
    }.get(str(value or "").upper(), OrderStatus.UNKNOWN)


def _order_event_type(status: OrderStatus) -> VenueEventType:
    return {
        OrderStatus.ACCEPTED: VenueEventType.ORDER_ACCEPTED,
        OrderStatus.PARTIALLY_FILLED: VenueEventType.ORDER_PARTIALLY_FILLED,
        OrderStatus.FILLED: VenueEventType.ORDER_FILLED,
        OrderStatus.CANCELED: VenueEventType.ORDER_CANCELED,
        OrderStatus.EXPIRED: VenueEventType.ORDER_EXPIRED,
        OrderStatus.REJECTED: VenueEventType.ORDER_REJECTED,
    }.get(status, VenueEventType.ORDER_UNKNOWN)


def _event_id(payload: dict[str, Any], identity: str, suffix: str) -> str:
    basis = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(basis.encode()).hexdigest()[:24]
    return f"binance:{identity}:{suffix}:{digest}"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed > 0 else None


def _millis_datetime(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)
