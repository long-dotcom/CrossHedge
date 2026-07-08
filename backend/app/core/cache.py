"""
通用 TTL 缓存模块

提供线程安全的通用 TTL（Time-To-Live）缓存，消除源项目中多种手写缓存模式的重复：
- ``mt5_sessions.py`` 的 ``_session_cache`` (cached_time + cached_value)
- ``live_costs.py`` 的 ``_hl_market_cache`` / ``_hl_user_fee_cache``
- ``scanner.py`` 的 ``_strategy_cache``

使用方式::

    from app.core.cache import TTLCache

    cache: TTLCache[dict] = TTLCache(ttl_seconds=30.0)
    cache.set("key", {"data": 123})
    value = cache.get("key")  # {"data": 123} 或 None（过期/不存在）
    cache.invalidate("key")
    cache.clear()
"""

from __future__ import annotations

import threading
import time
from typing import Generic, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """线程安全的通用 TTL 缓存。

    每个键值对在 ``set`` 时记录时间戳，``get`` 时检查是否过期。
    过期数据不会立即删除（惰性淘汰），而是在下次 ``get`` 时返回 ``None``。

    参数:
        ttl_seconds: 缓存有效期（秒），支持浮点数。
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        # 内部存储：key → (timestamp, value)
        self._store: dict[str, tuple[float, T]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> T | None:
        """获取缓存值。

        如果键不存在或已过期，返回 ``None``。

        参数:
            key: 缓存键。

        返回:
            缓存的值，或 ``None``。
        """
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            cached_at, value = entry
            if now - cached_at >= self._ttl:
                # 已过期，惰性删除
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: T) -> None:
        """写入或更新缓存值。

        参数:
            key: 缓存键。
            value: 要缓存的值。
        """
        with self._lock:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: str) -> None:
        """手动使指定键失效。

        参数:
            key: 要失效的缓存键。
        """
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """清空所有缓存。"""
        with self._lock:
            self._store.clear()
