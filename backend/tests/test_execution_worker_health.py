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


def test_worker_recovery_runs_all_reconciliation_steps(monkeypatch) -> None:
    calls: list[str] = []

    class Db:
        def commit(self) -> None:
            calls.append("commit")

    class Context:
        def __enter__(self):
            return Db()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(worker_main, "SessionLocal", lambda: Context())
    monkeypatch.setattr(worker_main, "sync_live_positions", lambda db, allow_remote_crypto: calls.append("positions"))
    monkeypatch.setattr(worker_main, "reconcile_execution_orders_once", lambda: calls.append("orders"))
    monkeypatch.setattr(worker_main, "reconcile_probe_runs_once", lambda: calls.append("probes"))

    worker_main._recover_execution_state()

    assert calls == ["positions", "commit", "orders", "probes"]
