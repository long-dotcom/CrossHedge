"""Binance Futures 增量订单簿状态机。"""

from __future__ import annotations

from decimal import Decimal
from threading import RLock
from typing import Any

from app.venues.domain.models import OrderBookSnapshot


class OrderBookGap(RuntimeError):
    """增量序列不连续，必须重新获取 REST 快照。"""


class BinanceLocalOrderBook:
    """按 Binance 官方 U/u/pu 规则维护单个品种的本地订单簿。"""

    def __init__(self, symbol: str, *, max_levels: int = 1000) -> None:
        self.symbol = symbol.upper()
        self.max_levels = max(int(max_levels), 20)
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._last_update_id: int | None = None
        self._synchronized = False
        self._lock = RLock()

    @property
    def synchronized(self) -> bool:
        with self._lock:
            return self._synchronized

    @property
    def last_update_id(self) -> int | None:
        with self._lock:
            return self._last_update_id

    def initialize(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self._bids = _levels(snapshot.get("bids", []))
            self._asks = _levels(snapshot.get("asks", []))
            self._last_update_id = int(snapshot["lastUpdateId"])
            self._synchronized = False

    def apply(self, event: dict[str, Any]) -> bool:
        """应用深度事件；过旧事件返回 False，序列缺口抛出异常。"""
        first = int(event.get("U", 0))
        final = int(event.get("u", 0))
        previous = int(event.get("pu", 0))
        with self._lock:
            if self._last_update_id is None:
                raise OrderBookGap("订单簿尚未初始化")
            if final < self._last_update_id:
                return False
            if not self._synchronized:
                if not (first <= self._last_update_id <= final):
                    if final < self._last_update_id:
                        return False
                    raise OrderBookGap(
                        f"首个增量未覆盖快照: U={first}, u={final}, snapshot={self._last_update_id}"
                    )
                self._synchronized = True
            elif previous != self._last_update_id:
                self._synchronized = False
                raise OrderBookGap(f"订单簿序列中断: pu={previous}, previous_u={self._last_update_id}")

            _apply_levels(self._bids, event.get("b", []))
            _apply_levels(self._asks, event.get("a", []))
            self._last_update_id = final
            self._trim()
            return True

    def snapshot(self, *, depth: int = 20, exchange_time=None) -> OrderBookSnapshot:
        with self._lock:
            bids = tuple(sorted(self._bids.items(), reverse=True)[:depth])
            asks = tuple(sorted(self._asks.items())[:depth])
            return OrderBookSnapshot(
                venue="binance",
                symbol=self.symbol,
                bids=bids,
                asks=asks,
                sequence=self._last_update_id,
                exchange_time=exchange_time,
                synchronized=self._synchronized,
            )

    def invalidate(self) -> None:
        with self._lock:
            self._synchronized = False
            self._last_update_id = None
            self._bids.clear()
            self._asks.clear()

    def _trim(self) -> None:
        if len(self._bids) > self.max_levels:
            keep = set(sorted(self._bids, reverse=True)[: self.max_levels])
            self._bids = {price: quantity for price, quantity in self._bids.items() if price in keep}
        if len(self._asks) > self.max_levels:
            keep = set(sorted(self._asks)[: self.max_levels])
            self._asks = {price: quantity for price, quantity in self._asks.items() if price in keep}


def _levels(rows) -> dict[Decimal, Decimal]:
    values: dict[Decimal, Decimal] = {}
    _apply_levels(values, rows)
    return values


def _apply_levels(target: dict[Decimal, Decimal], rows) -> None:
    for row in rows or []:
        try:
            price = Decimal(str(row[0]))
            quantity = Decimal(str(row[1]))
        except (IndexError, TypeError, ValueError):
            continue
        if quantity == 0:
            target.pop(price, None)
        elif price > 0 and quantity > 0:
            target[price] = quantity
