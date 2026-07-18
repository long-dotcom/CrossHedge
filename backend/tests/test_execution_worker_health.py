"""独立执行 Worker 心跳在 Windows 文件争用下的容错测试。"""

from pathlib import Path

from app.execution import worker_main


def test_worker_health_retries_windows_permission_error(monkeypatch, tmp_path) -> None:
    target = tmp_path / "execution-worker-health.json"
    monkeypatch.setattr(worker_main, "HEALTH_PATH", target)
    original_replace = Path.replace
    attempts = {"count": 0}

    def flaky_replace(path: Path, destination: Path):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise PermissionError(5, "文件被短暂占用")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    assert worker_main._write_health(status="ok", processed=2) is True
    assert target.exists()
    assert attempts["count"] == 3


def test_worker_health_failure_does_not_raise_or_stop_execution(monkeypatch, tmp_path) -> None:
    target = tmp_path / "execution-worker-health.json"
    monkeypatch.setattr(worker_main, "HEALTH_PATH", target)
    monkeypatch.setattr(Path, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(5, "持续占用")))

    assert worker_main._write_health(status="degraded", error="test") is False
    assert not target.exists()
