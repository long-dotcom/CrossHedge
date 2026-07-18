"""Connector 工厂注册表。"""

from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from app.venues.protocols import VenueConnector

ConnectorFactory = Callable[..., VenueConnector]


class VenueRegistry:
    """线程安全的 Connector 工厂表，新增交易所不需要修改业务分支。"""

    def __init__(self) -> None:
        self._factories: dict[str, ConnectorFactory] = {}
        self._lock = RLock()

    def register(self, venue: str, factory: ConnectorFactory, *, replace: bool = False) -> None:
        key = self.normalize(venue)
        with self._lock:
            if key in self._factories and not replace:
                raise ValueError(f"交易场所已经注册: {key}")
            self._factories[key] = factory

    def unregister(self, venue: str) -> None:
        with self._lock:
            self._factories.pop(self.normalize(venue), None)

    def create(self, venue: str, **kwargs) -> VenueConnector:
        key = self.normalize(venue)
        with self._lock:
            factory = self._factories.get(key)
        if factory is None:
            raise ValueError(f"尚未接入原生交易场所: {key}")
        return factory(**kwargs)

    def venues(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._factories))

    @staticmethod
    def normalize(venue: str) -> str:
        value = str(venue or "").strip().lower()
        if not value:
            raise ValueError("交易场所不能为空")
        return value


venue_registry = VenueRegistry()
