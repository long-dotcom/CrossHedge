"""基于 Redis 的通用 TTL 缓存。"""

from __future__ import annotations

import base64
import pickle
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
