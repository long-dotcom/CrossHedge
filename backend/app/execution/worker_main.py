"""独立执行 Worker 进程入口。

FastAPI 只创建 Intent/Outbox；本进程独占交易副作用、私有事件和订单回查。
进程间通过数据库 Outbox 传递命令，并用健康文件向 API 暴露存活状态。
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

from app.config.settings import ROOT_DIR, enforce_runtime_security, get_settings
from app.core.logging import get_logger, setup_logging
from app.core.time_utils import utc_now
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.execution.outbox_worker import run_execution_outbox_once
from app.execution.venue_events import enqueue_venue_event
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)
HEALTH_PATH = ROOT_DIR / ".run" / "execution-worker-health.json"
_running = True


def _request_stop(*_args) -> None:
    global _running
    _running = False


def _write_health(*, status: str, error: str = "", processed: int = 0) -> bool:
    """写入 Worker 心跳；Windows 短暂占用目标文件时重试但不中断交易循环。"""
    HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "pid": os.getpid(),
        "updated_at": utc_now().isoformat(),
        "last_error": error,
        "last_processed_count": processed,
        "venue_runtimes": native_venue_manager.health_snapshot(),
    }
    # PID 和线程 ID 避免新旧 Worker 交接时同时争用同一个临时文件。
    temporary = HEALTH_PATH.with_name(
        f"{HEALTH_PATH.stem}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            temporary.replace(HEALTH_PATH)
            return True
        except PermissionError as exc:
            # Windows 的杀毒软件或健康检查读取可能在极短时间内持有文件句柄。
            last_error = exc
            time.sleep(0.02 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            break
    try:
        temporary.unlink(missing_ok=True)
    except OSError:
        pass
    logger.warning("执行 Worker 心跳写入失败，执行循环继续运行: path={}, error={}", HEALTH_PATH, last_error)
    return False


def main() -> None:
    settings = get_settings()
    setup_logging(settings.environment)
    enforce_runtime_security(settings)
    init_db()
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        native_venue_manager.add_event_handler(enqueue_venue_event)
        try:
            native_venue_manager.preload_enabled(start=True, subscribe_market_data=False)
        except Exception as exc:
            logger.exception("执行 Worker 的原生连接器预热失败，继续提供 Paper/可用 venue 服务: {}", exc)
            _write_health(status="degraded", error=str(exc))
        next_health_at = 0.0
        while _running:
            try:
                processed = run_execution_outbox_once()
                now = time.monotonic()
                if now >= next_health_at:
                    runtimes = native_venue_manager.health_snapshot()
                    degraded = any(row.get("status") != "ok" for row in runtimes)
                    _write_health(
                        status="degraded" if degraded else "ok",
                        error="部分交易所连接器处于降级状态" if degraded else "",
                        processed=processed,
                    )
                    next_health_at = now + 1.0
            except Exception as exc:
                logger.exception("独立执行 Worker 循环失败: {}", exc)
                _write_health(status="degraded", error=str(exc))
            time.sleep(0.05)
    finally:
        native_venue_manager.reset()
        _write_health(status="stopped")


if __name__ == "__main__":
    main()
