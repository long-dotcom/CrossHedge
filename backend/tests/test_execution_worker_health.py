"""独立执行 Worker Redis 心跳测试。"""

import json

from app.core.redis_client import redis_client
from app.execution import worker_main
from app.main import _execution_worker_health


def test_worker_health_is_shared_through_redis() -> None:
    assert worker_main._write_health(status="ok", processed=2) is True
    stored = json.loads(redis_client().get(worker_main.HEALTH_KEY))
    assert stored["last_processed_count"] == 2
    health = _execution_worker_health()
    assert health["status"] == "ok"
    assert health["stale"] is False


def test_worker_health_failure_does_not_raise_or_stop_execution(monkeypatch) -> None:
    monkeypatch.setattr(worker_main, "redis_client", lambda: (_ for _ in ()).throw(ConnectionError("Redis 不可用")))

    assert worker_main._write_health(status="degraded", error="test") is False
