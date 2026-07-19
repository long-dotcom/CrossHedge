"""基于私有 WebSocket 事件等待订单状态和成交明细。"""

from __future__ import annotations

import threading
import time
from decimal import Decimal

from app.venues.domain.events import VenueEvent
from app.venues.domain.models import Fill, OrderSnapshot, OrderStatus


TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.EXPIRED,
    OrderStatus.REJECTED,
}


class OrderEventWaiter:
    """缓存私有流中的订单事实，并用条件变量唤醒等待中的执行线程。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._orders_by_client: dict[str, OrderSnapshot] = {}
        self._orders_by_venue: dict[str, OrderSnapshot] = {}
        self._fills: dict[str, Fill] = {}

    def on_event(self, event: VenueEvent) -> None:
        with self._condition:
            if event.order is not None:
                self.seed(event.order)
            if event.fill is not None:
                fill = event.fill
                key = f"{fill.venue}:{fill.symbol}:{fill.trade_id}"
                self._fills[key] = fill
            self._condition.notify_all()

    def seed(self, snapshot: OrderSnapshot) -> None:
        """登记提交响应，避免 WS 事件先到或后到时发生竞态。"""
        with self._condition:
            if snapshot.client_order_id:
                self._orders_by_client[snapshot.client_order_id] = snapshot
            if snapshot.venue_order_id:
                self._orders_by_venue[snapshot.venue_order_id] = snapshot
            self._condition.notify_all()

    def latest(self, client_order_id: str, venue_order_id: str = "") -> OrderSnapshot | None:
        with self._condition:
            return self._latest_unlocked(client_order_id, venue_order_id)

    def wait_for_terminal(
        self,
        *,
        client_order_id: str,
        venue_order_id: str = "",
        timeout_seconds: float,
    ) -> OrderSnapshot:
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        with self._condition:
            while True:
                snapshot = self._latest_unlocked(client_order_id, venue_order_id)
                if snapshot is not None and snapshot.status in TERMINAL_ORDER_STATUSES:
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"私有 WebSocket 未在 {timeout_seconds:g}s 内确认订单终态: {client_order_id}"
                    )
                self._condition.wait(remaining)

    def wait_until(
        self,
        *,
        client_order_id: str,
        venue_order_id: str = "",
        timeout_seconds: float,
        predicate,
    ) -> OrderSnapshot | None:
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        with self._condition:
            while True:
                snapshot = self._latest_unlocked(client_order_id, venue_order_id)
                if snapshot is not None and predicate(snapshot):
                    return snapshot
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return snapshot
                self._condition.wait(remaining)

    def fill_details(
        self,
        *,
        client_order_id: str,
        venue_order_id: str = "",
    ) -> tuple[Decimal, Decimal]:
        with self._condition:
            return self._fill_details_unlocked(client_order_id, venue_order_id)

    def _fill_details_unlocked(self, client_order_id: str, venue_order_id: str) -> tuple[Decimal, Decimal]:
        fills = [
            fill for fill in self._fills.values()
            if (client_order_id and fill.client_order_id == client_order_id)
            or (venue_order_id and fill.venue_order_id == venue_order_id)
        ]
        quantity = sum((abs(Decimal(str(fill.quantity))) for fill in fills), Decimal("0"))
        notional = sum(
            (abs(Decimal(str(fill.quantity))) * Decimal(str(fill.price)) for fill in fills),
            Decimal("0"),
        )
        return quantity, notional / quantity if quantity > 0 else Decimal("0")

    def wait_for_fill_details(
        self,
        *,
        client_order_id: str,
        venue_order_id: str = "",
        minimum_quantity: Decimal = Decimal("0"),
        timeout_seconds: float = 2.0,
    ) -> tuple[Decimal, Decimal]:
        """等待独立成交事件；订单终态与成交推送分属不同频道时可消除竞态。"""
        deadline = time.monotonic() + max(float(timeout_seconds), 0.0)
        with self._condition:
            while True:
                details = self._fill_details_unlocked(client_order_id, venue_order_id)
                if details[0] > 0 and details[0] + Decimal("1e-12") >= minimum_quantity:
                    return details
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return details
                self._condition.wait(remaining)

    def _latest_unlocked(self, client_order_id: str, venue_order_id: str) -> OrderSnapshot | None:
        return (
            self._orders_by_client.get(client_order_id)
            or self._orders_by_venue.get(venue_order_id)
        )
