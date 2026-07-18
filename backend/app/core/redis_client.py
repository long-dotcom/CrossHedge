"""Redis 连接与 CrossHedge 键名约定。

业务后端只通过这里创建 Redis 客户端，避免模块自行维护连接池。生产运行时
Redis 是必需依赖；连接异常会显式抛出，防止交易系统静默退回进程内缓存。
"""

from __future__ import annotations

from functools import lru_cache

from redis import Redis

from app.config.settings import get_settings


@lru_cache
def redis_client() -> Redis:
    settings = get_settings().redis
    if get_settings().environment.strip().lower() in {"test", "testing"}:
        import fakeredis
        return fakeredis.FakeRedis(decode_responses=True)
    return Redis.from_url(
        settings.url,
        decode_responses=True,
        socket_connect_timeout=settings.socket_timeout_seconds,
        socket_timeout=settings.socket_timeout_seconds,
        health_check_interval=30,
    )


def redis_key(*parts: object) -> str:
    """生成带应用前缀的 Redis 键。"""
    prefix = get_settings().redis.key_prefix.strip(":") or "crosshedge"
    values = [prefix, *(str(part).strip(":") for part in parts)]
    return ":".join(value for value in values if value)


def verify_redis() -> None:
    """启动期验证 Redis 可用性。"""
    if not redis_client().ping():
        raise RuntimeError("Redis PING 未返回成功")
