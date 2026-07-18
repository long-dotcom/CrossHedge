"""
CrossHedge FastAPI 应用入口
============================

创建 FastAPI 实例，注册中间件、路由，以及启动/关闭事件。

启动事件按顺序执行：
1. 运行时安全检查（生产环境强制使用强密钥）
2. 数据库初始化
3. 原生交易所连接器与首条行情预热
4. 行情数据管理器启动
5. 一系列启动步骤（MT5 会话同步、信号缓存、扫描等）
6. 定时调度器启动

关闭事件：
1. 停止定时调度器
2. 停止行情数据管理器
"""

from __future__ import annotations

import time
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import create_api_router
from app.config.settings import ROOT_DIR, enforce_runtime_security, get_settings
from app.core.logging import get_logger, setup_logging
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.execution.auto_closer import run_auto_close
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.hedge_pool import hedge_pool
from app.execution.reconciler import run_execution_reconcile
from app.market.mt5_schedule import sync_mt5_session_templates
from app.market.mt5_tradability import refresh_mt5_tradability_cache
from app.market.scanner import persist_scan_state, run_scan
from app.strategy.statistical_signal import refresh_signal_stats_cache
from app.workers.market_data import market_data_manager
from app.workers.scheduler import start_scheduler, stop_scheduler
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# FastAPI 实例
# ---------------------------------------------------------------------------

app = FastAPI(title="CrossHedge — Hyperliquid + MT5 Hedge API")

# CORS 中间件：允许前端开发服务器跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册所有 API 路由
app.include_router(create_api_router())


# ---------------------------------------------------------------------------
# 启动事件
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup() -> None:
    """应用启动时执行初始化流程。"""
    settings = get_settings()

    # 初始化日志系统
    setup_logging(settings.environment)

    # 运行时安全检查（生产环境强制使用强密钥）
    enforce_runtime_security(settings)

    # 数据库初始化（创建表 / 插入默认数据）
    init_db()

    # 原生连接器先建立公共行情连接，再启动统一行情缓存任务。
    db = SessionLocal()
    try:
        _startup_step(
            db,
            "preload_native_market_data",
            lambda: native_venue_manager.preload_enabled(start=True, subscribe_market_data=True),
        )

        # 预热完成后才允许各行情线程和扫描器启动。
        market_data_manager.start()
        market_data_manager.wait_until_seeded(
            timeout_seconds=settings.venues.startup_timeout_seconds,
        )

        # 启动时依次执行初始化步骤，每步失败不影响后续步骤
        _startup_step(db, "sync_mt5_session_templates", lambda: sync_mt5_session_templates(db, only_auto=True))
        _startup_step(db, "refresh_signal_stats_cache", lambda: refresh_signal_stats_cache(db))
        _startup_step(db, "refresh_mt5_tradability_cache", lambda: refresh_mt5_tradability_cache(db))
        _startup_step(db, "load_hedge_pool", lambda: hedge_pool.load_from_db(db))
        _startup_step(db, "run_scan", lambda: run_scan(db))
        _startup_step(db, "persist_scan_state", lambda: persist_scan_state(db))
        _startup_step(db, "run_carry_cost_sync", lambda: run_carry_cost_sync(db, force=True))
        _startup_step(db, "run_auto_close", lambda: run_auto_close(db))
        _startup_step(db, "run_execution_reconcile", lambda: run_execution_reconcile(db))
    finally:
        db.close()

    # 启动定时调度器
    start_scheduler()
    logger.info("CrossHedge 启动完成")


# ---------------------------------------------------------------------------
# 关闭事件
# ---------------------------------------------------------------------------

@app.on_event("shutdown")
def on_shutdown() -> None:
    """应用关闭时清理资源。"""
    stop_scheduler()
    native_venue_manager.reset()
    market_data_manager.stop()
    logger.info("CrossHedge 已关闭")


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, object]:
    """健康检查端点（K8s / 负载均衡器使用）。"""
    runtimes = native_venue_manager.health_snapshot()
    venue_degraded = any(row.get("status") != "ok" for row in runtimes)
    worker = _execution_worker_health()
    worker_healthy = worker.get("status") in {"ok", "degraded"} and not worker.get("stale", True)
    return {
        "status": "degraded" if venue_degraded or not worker_healthy else "ok",
        "venue_runtimes": runtimes,
        "execution_worker": worker,
    }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _startup_step(db, name: str, func) -> None:
    """执行单个启动步骤，失败时回滚并记录日志，不中断后续步骤。"""
    started_at = time.monotonic()
    logger.info("启动任务开始: {}", name)
    try:
        func()
    except Exception as exc:
        db.rollback()
        logger.exception(
            "启动任务失败，已跳过: {}; duration_seconds={:.2f}; {}",
            name,
            time.monotonic() - started_at,
            exc,
        )
    else:
        logger.info(
            "启动任务完成: {}; duration_seconds={:.2f}",
            name,
            time.monotonic() - started_at,
        )


def _execution_worker_health() -> dict[str, object]:
    """读取独立执行 Worker 心跳；超过 5 秒即视为失联。"""
    path = Path(ROOT_DIR) / ".run" / "execution-worker-health.json"
    if not path.exists():
        return {"status": "missing", "stale": True, "last_error": "执行 Worker 尚无心跳"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        updated_at = datetime.fromisoformat(str(payload.get("updated_at") or ""))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        payload["age_seconds"] = max((datetime.now(timezone.utc) - updated_at).total_seconds(), 0.0)
        payload["stale"] = payload["age_seconds"] > 5.0
        return payload
    except Exception as exc:
        return {"status": "invalid", "stale": True, "last_error": str(exc)}
