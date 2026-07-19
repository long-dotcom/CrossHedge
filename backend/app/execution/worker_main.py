"""独立执行 Worker 进程入口。

FastAPI 只创建 Intent/Outbox；本进程独占交易副作用、私有事件和订单回查。
进程间通过数据库 Outbox 传递命令，并用 Redis 心跳向 API 暴露存活状态。
"""

from __future__ import annotations

import json
import os
import signal
import time

from app.config.settings import enforce_runtime_security, get_settings
from app.core.logging import get_logger, setup_logging
from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.execution.outbox_worker import reconcile_execution_orders_once, run_execution_outbox_once
from app.execution.probe_runs import reconcile_probe_runs_once
from app.execution.reconciler import sync_live_positions
from app.execution.venue_events import enqueue_venue_event
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)
HEALTH_KEY = redis_key("health", "execution-worker")
_running = True


def _request_stop(*_args) -> None:
    global _running
    _running = False


def _write_health(*, status: str, error: str = "", processed: int = 0) -> bool:
    """写入 Redis Worker 心跳；失败时不中断交易循环。"""
    payload = {
        "status": status,
        "pid": os.getpid(),
        "updated_at": utc_now().isoformat(),
        "last_error": error,
        "last_processed_count": processed,
        "venue_runtimes": native_venue_manager.health_snapshot(),
    }
    try:
        redis_client().set(HEALTH_KEY, json.dumps(payload, ensure_ascii=False), ex=10)
        return True
    except Exception as exc:
        logger.warning("执行 Worker Redis 心跳写入失败，执行循环继续运行: {}", exc)
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
        # 进程启动只执行一次恢复快照；正常运行完全由账户私有 WS 推进。
        try:
            with SessionLocal() as db:
                # Binance 用户流没有初始账户快照；启动时仅拉取一次，之后由 WS 增量维护。
                sync_live_positions(db, allow_remote_crypto=True)
                db.commit()
            reconcile_execution_orders_once()
            reconcile_probe_runs_once()
        except Exception as exc:
            logger.exception("执行 Worker 启动恢复对账失败，等待私有 WS 重连后重试: {}", exc)
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
                    # 心跳每 2 秒刷新；各连接器 health() 只能读取本地状态，禁止发远端探测请求。
                    next_health_at = now + 2.0
            except Exception as exc:
                logger.exception("独立执行 Worker 循环失败: {}", exc)
                _write_health(status="degraded", error=str(exc))
            time.sleep(0.05)
    finally:
        native_venue_manager.reset()
        _write_health(status="stopped")


if __name__ == "__main__":
    main()
