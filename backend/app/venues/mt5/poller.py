"""MT5 活动订单增量轮询器。"""

from __future__ import annotations

import hashlib
import threading
import time

from app.core.logging import get_logger
from app.venues.domain.events import VenueEvent, VenueEventType
from app.venues.domain.models import OrderSnapshot, OrderStatus
from app.venues.protocols import EventHandler

logger = get_logger(__name__)


class MT5OrderPoller:
    """只轮询当前进程提交或恢复登记的活动订单。"""

    def __init__(self, connector, *, interval_ms: int = 75) -> None:
        self.connector = connector
        self.interval = max(int(interval_ms), 25) / 1000
        self._orders: dict[str, tuple[str, str]] = {}
        self._last_state: dict[str, tuple[str, float]] = {}
        self._seen_trades: set[str] = set()
        self._handlers: list[EventHandler] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def add_handler(self, handler: EventHandler) -> None:
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)
        self.start()

    def watch(self, symbol: str, venue_order_id: str, client_order_id: str = "") -> None:
        if not venue_order_id:
            return
        with self._lock:
            self._orders[venue_order_id] = (symbol, client_order_id)
        self.start()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mt5-order-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)

    def health(self) -> dict:
        with self._lock:
            active = len(self._orders)
        return {
            "poller_running": bool(self._thread and self._thread.is_alive()),
            "active_orders": active,
            "poll_interval_ms": int(self.interval * 1000),
        }

    def poll_once(self) -> int:
        emitted = 0
        with self._lock:
            watched = dict(self._orders)
        for venue_order_id, (symbol, client_order_id) in watched.items():
            try:
                order = self.connector.get_order(
                    symbol,
                    venue_order_id=venue_order_id,
                    client_order_id=client_order_id,
                )
                state = (order.status.value, float(order.filled_quantity))
                if self._last_state.get(venue_order_id) != state:
                    self._last_state[venue_order_id] = state
                    self._emit_order(order)
                    emitted += 1
                for fill in self.connector.get_fills(
                    symbol,
                    venue_order_id=venue_order_id,
                    client_order_id=client_order_id,
                ):
                    if fill.trade_id in self._seen_trades:
                        continue
                    self._seen_trades.add(fill.trade_id)
                    self._emit(
                        VenueEvent(
                            f"mt5:deal:{fill.trade_id}",
                            "mt5",
                            VenueEventType.FILL,
                            fill.occurred_at,
                            fill=fill,
                        )
                    )
                    emitted += 1
                if order.status in {
                    OrderStatus.FILLED,
                    OrderStatus.CANCELED,
                    OrderStatus.EXPIRED,
                    OrderStatus.REJECTED,
                }:
                    with self._lock:
                        self._orders.pop(venue_order_id, None)
            except Exception as exc:
                logger.warning("MT5 活动订单轮询失败: order={}, error={}", venue_order_id, exc)
        return emitted

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def _emit_order(self, order: OrderSnapshot) -> None:
        event_type = {
            OrderStatus.ACCEPTED: VenueEventType.ORDER_ACCEPTED,
            OrderStatus.PARTIALLY_FILLED: VenueEventType.ORDER_PARTIALLY_FILLED,
            OrderStatus.FILLED: VenueEventType.ORDER_FILLED,
            OrderStatus.CANCELED: VenueEventType.ORDER_CANCELED,
            OrderStatus.EXPIRED: VenueEventType.ORDER_EXPIRED,
            OrderStatus.REJECTED: VenueEventType.ORDER_REJECTED,
        }.get(order.status, VenueEventType.ORDER_UNKNOWN)
        basis = f"{order.venue_order_id}:{order.status}:{order.filled_quantity}"
        digest = hashlib.sha256(basis.encode()).hexdigest()[:32]
        self._emit(VenueEvent(f"mt5:order:{digest}", "mt5", event_type, order.updated_at, order=order))

    def _emit(self, event: VenueEvent) -> None:
        with self._lock:
            handlers = tuple(self._handlers)
        for handler in handlers:
            handler(event)
