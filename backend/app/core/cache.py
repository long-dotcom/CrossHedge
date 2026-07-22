"""基于 Redis 的通用 TTL 缓存。"""

from __future__ import annotations

import base64
import pickle
from threading import RLock
from time import monotonic
from typing import Generic, TypeVar

from app.core.redis_client import redis_client, redis_key

T = TypeVar("T")


class TTLCache(Generic[T]):
    """保持原调用接口的 Redis TTL 缓存。

    值可能是 dataclass、SimpleNamespace 等内部 Python 对象，因此使用带 Base64
    包装的 pickle。键名前缀固定且 Redis 只允许受信网络访问。
    """

    def __init__(self, ttl_seconds: float, namespace: str = "default") -> None:
        self._ttl = max(float(ttl_seconds), 0.001)
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return redis_key("cache", self._namespace, key)

    def get(self, key: str) -> T | None:
        raw = redis_client().get(self._key(key))
        if raw is None:
            return None
        return pickle.loads(base64.b64decode(raw.encode("ascii")))

    def get_many(self, keys: list[str]) -> dict[str, T]:
        """一次 Redis 往返批量读取多个缓存值。"""
        if not keys:
            return {}
        raws = redis_client().mget([self._key(key) for key in keys])
        return {
            key: pickle.loads(base64.b64decode(raw.encode("ascii")))
            for key, raw in zip(keys, raws)
            if raw is not None
        }

    def set(self, key: str, value: T) -> None:
        raw = base64.b64encode(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")
        redis_client().set(self._key(key), raw, px=max(int(self._ttl * 1000), 1))

    def invalidate(self, key: str) -> None:
        redis_client().delete(self._key(key))

    def clear(self) -> None:
        cursor: int | str = 0
        pattern = self._key("*")
        while True:
            cursor, keys = redis_client().scan(cursor=cursor, match=pattern, count=200)
            if keys:
                redis_client().delete(*keys)
            if int(cursor) == 0:
                break


class LocalTTLCache(Generic[T]):
    """线程安全的进程内 TTL 缓存。

    适用于由本进程主动失效、且无需借助 Redis 共享每次读取结果的配置快照。
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = max(float(ttl_seconds), 0.001)
        self._values: dict[str, tuple[float, T]] = {}
        self._lock = RLock()

    def get(self, key: str) -> T | None:
        now = monotonic()
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= now:
                self._values.pop(key, None)
                return None
            return value

    def set(self, key: str, value: T) -> None:
        with self._lock:
            self._values[key] = (monotonic() + self._ttl, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
