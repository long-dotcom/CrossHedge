"""
定时任务调度器模块
==================

基于 ``threading.Timer`` 的链式定时任务调度，管理以下周期性任务：

- **scanner_job** — 主扫描循环（价差扫描）
- **scan_persistence_job** — 扫描状态持久化
- **execution_maintenance_job** — 执行维护（自动开仓 + 自动平仓 + 对账）
- **carry_cost_job** — 资金费/过夜费同步
- 执行 Outbox 由独立 OS Worker 处理，本调度器禁止触碰交易所
- **signal_stats_job** — 统计信号缓存刷新
- **mt5_tradability_job** — MT5 交易能力缓存刷新
- **mt5_session_template_job** — MT5 交易时段模板同步
- **cb_config_job** — 断路器配置刷新
- **venue_metadata_job** — 品种规格、费率和资金费刷新
- **account_snapshot_job** — 账户余额定时兜底快照

使用 ``db_session`` 替代 ``SessionLocal + try/except``，
使用 ``get_logger`` 替代 ``from loguru import logger``。

使用方式::

    from app.workers.scheduler import start_scheduler, stop_scheduler

    start_scheduler()   # 启动所有定时任务
    stop_scheduler()    # 停止所有定时任务
"""

from __future__ import annotations

import threading
from typing import Optional

from app.config.settings import get_settings
from app.core.db_session import db_session
from app.core.logging import get_logger
from app.execution.auto_closer import run_auto_close
from app.execution.auto_executor import run_auto_execute
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.circuit_breaker import reload_config as reload_cb_config
from app.execution.reconciler import run_execution_reconcile
from app.market.mt5_schedule import sync_mt5_session_templates
from app.market.mt5_tradability import refresh_mt5_tradability_cache
from app.market.scanner import persist_scan_state, run_scan
from app.strategy.statistical_signal import refresh_signal_stats_cache
from app.accounts.sync import sync_account_snapshots
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 定时器引用与互斥标志
# ---------------------------------------------------------------------------

_timer: Optional[threading.Timer] = None
_stats_timer: Optional[threading.Timer] = None
_tradability_timer: Optional[threading.Timer] = None
_session_template_timer: Optional[threading.Timer] = None
_scan_persistence_timer: Optional[threading.Timer] = None
_execution_timer: Optional[threading.Timer] = None
_carry_cost_timer: Optional[threading.Timer] = None
_cb_config_timer: Optional[threading.Timer] = None
_venue_metadata_timer: Optional[threading.Timer] = None
_account_snapshot_timer: Optional[threading.Timer] = None

_running = False
_stats_refreshing = False
_tradability_refreshing = False
_session_template_refreshing = False
_scan_persisting = False
_execution_running = False
_carry_cost_running = False
_cb_config_running = False
_venue_metadata_running = False
_account_snapshot_running = False


# ---------------------------------------------------------------------------
# 定时任务函数
# ---------------------------------------------------------------------------

def scanner_job() -> None:
    """主扫描循环 —— 定期扫描套利机会。"""
    try:
        with db_session() as db:
            run_scan(db)
    except Exception as exc:
        logger.exception("扫描任务失败: {}", exc)
    _schedule_next()


def execution_maintenance_job() -> None:
    """执行维护任务 —— 自动开仓 + 自动平仓 + 对账。"""
    global _execution_running
    if _execution_running:
        _schedule_next_execution()
        return
    _execution_running = True
    try:
        # 三个阶段使用独立事务，单个阶段失败不再阻断后续平仓或对账。
        for name, task in (
            ("auto_execute", run_auto_execute),
            ("auto_close", run_auto_close),
            ("execution_reconcile", run_execution_reconcile),
        ):
            try:
                with db_session() as db:
                    task(db)
            except Exception as exc:
                logger.exception("执行维护阶段失败: stage={}, error={}", name, exc)
    finally:
        _execution_running = False
    _schedule_next_execution()


def carry_cost_job() -> None:
    """资金费/过夜费同步任务。"""
    global _carry_cost_running
    if _carry_cost_running:
        _schedule_next_carry_cost()
        return
    _carry_cost_running = True
    try:
        with db_session() as db:
            run_carry_cost_sync(db)
    except Exception as exc:
        logger.exception("资金费/过夜费同步任务失败: {}", exc)
    finally:
        _carry_cost_running = False
    _schedule_next_carry_cost()


def scan_persistence_job() -> None:
    """扫描状态持久化任务。"""
    global _scan_persisting
    if _scan_persisting:
        _schedule_next_scan_persistence()
        return
    _scan_persisting = True
    try:
        with db_session() as db:
            persist_scan_state(db)
    except Exception as exc:
        logger.exception("扫描状态持久化失败: {}", exc)
    finally:
        _scan_persisting = False
    _schedule_next_scan_persistence()


def signal_stats_job() -> None:
    """统计信号缓存刷新任务。"""
    global _stats_refreshing
    if _stats_refreshing:
        _schedule_next_stats()
        return
    _stats_refreshing = True
    try:
        with db_session() as db:
            refresh_signal_stats_cache(db)
    except Exception as exc:
        logger.exception("统计线刷新任务失败: {}", exc)
    finally:
        _stats_refreshing = False
    _schedule_next_stats()


def mt5_tradability_job() -> None:
    """MT5 交易能力缓存刷新任务。"""
    global _tradability_refreshing
    if _tradability_refreshing:
        _schedule_next_tradability()
        return
    _tradability_refreshing = True
    try:
        with db_session() as db:
            refresh_mt5_tradability_cache(db)
    except Exception as exc:
        logger.exception("MT5 交易能力刷新任务失败: {}", exc)
    finally:
        _tradability_refreshing = False
    _schedule_next_tradability()


def mt5_session_template_job() -> None:
    """MT5 交易时段模板同步任务。"""
    global _session_template_refreshing
    if _session_template_refreshing:
        _schedule_next_session_templates()
        return
    _session_template_refreshing = True
    try:
        with db_session() as db:
            sync_mt5_session_templates(db, only_auto=True)
    except Exception as exc:
        logger.exception("MT5 交易时段模板刷新任务失败: {}", exc)
    finally:
        _session_template_refreshing = False
    _schedule_next_session_templates()


def cb_config_job() -> None:
    """断路器配置刷新任务。"""
    global _cb_config_running
    if _cb_config_running:
        _schedule_next_cb_config()
        return
    _cb_config_running = True
    try:
        with db_session() as db:
            reload_cb_config(db)
    except Exception as exc:
        logger.exception("断路器配置刷新任务失败: {}", exc)
    finally:
        _cb_config_running = False
    _schedule_next_cb_config()


def venue_metadata_job() -> None:
    """定时刷新最小交易量、步进、手续费和资金费。"""
    global _venue_metadata_running
    if _venue_metadata_running:
        _schedule_next_venue_metadata()
        return
    _venue_metadata_running = True
    try:
        for venue in ("hyperliquid", "mt5", "binance"):
            try:
                connector = native_venue_manager.connector_for(venue, "live")
                for symbol in native_venue_manager.configured_symbols(venue):
                    connector.get_instrument(symbol, refresh=True)
            except Exception as exc:
                logger.warning("交易所元数据刷新失败: venue={}, error={}", venue, exc)
    finally:
        _venue_metadata_running = False
    _schedule_next_venue_metadata()


def account_snapshot_job() -> None:
    """定时保存账户余额快照，作为私有事件之外的兜底对账。"""
    global _account_snapshot_running
    if _account_snapshot_running:
        _schedule_next_account_snapshot()
        return
    _account_snapshot_running = True
    try:
        with db_session() as db:
            sync_account_snapshots(db)
    except Exception as exc:
        logger.exception("账户快照同步失败: {}", exc)
    finally:
        _account_snapshot_running = False
    _schedule_next_account_snapshot()


# ---------------------------------------------------------------------------
# 调度函数
# ---------------------------------------------------------------------------

def _schedule_next() -> None:
    """调度下一轮主扫描。"""
    global _timer
    if not _running:
        return
    settings = get_settings()
    interval = settings.scanner.interval_ms / 1000 if settings.scanner.interval_ms > 0 else settings.scanner.interval_seconds
    _timer = threading.Timer(max(interval, 0.05), scanner_job)
    _timer.daemon = True
    _timer.start()


def _schedule_next_stats() -> None:
    """调度下一轮统计信号刷新。"""
    global _stats_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.quote.signal_stats_cache_ttl_ms / 1000, 1.0)
    _stats_timer = threading.Timer(interval, signal_stats_job)
    _stats_timer.daemon = True
    _stats_timer.start()


def _schedule_next_scan_persistence() -> None:
    """调度下一轮扫描状态持久化。"""
    global _scan_persistence_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.scanner.persist_interval_ms / 1000, 0.1)
    _scan_persistence_timer = threading.Timer(interval, scan_persistence_job)
    _scan_persistence_timer.daemon = True
    _scan_persistence_timer.start()


def _schedule_next_execution() -> None:
    """调度下一轮执行维护。"""
    global _execution_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.scanner.execution_maintenance_interval_ms / 1000, 0.2)
    _execution_timer = threading.Timer(interval, execution_maintenance_job)
    _execution_timer.daemon = True
    _execution_timer.start()


def _schedule_next_carry_cost() -> None:
    """调度下一轮资金费同步。"""
    global _carry_cost_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.cost.carry_cost_sync_interval_seconds, 1)
    _carry_cost_timer = threading.Timer(interval, carry_cost_job)
    _carry_cost_timer.daemon = True
    _carry_cost_timer.start()


def _schedule_next_tradability() -> None:
    """调度下一轮 MT5 交易能力刷新。"""
    global _tradability_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.mt5.tradability_refresh_seconds, 1)
    _tradability_timer = threading.Timer(interval, mt5_tradability_job)
    _tradability_timer.daemon = True
    _tradability_timer.start()


def _schedule_next_session_templates() -> None:
    """调度下一轮 MT5 交易时段模板同步。"""
    global _session_template_timer
    if not _running:
        return
    settings = get_settings()
    interval = max(settings.mt5.session_template_refresh_hours, 1) * 3600
    _session_template_timer = threading.Timer(interval, mt5_session_template_job)
    _session_template_timer.daemon = True
    _session_template_timer.start()


def _schedule_next_cb_config() -> None:
    """调度下一轮断路器配置刷新。"""
    global _cb_config_timer
    if not _running:
        return
    _cb_config_timer = threading.Timer(60.0, cb_config_job)
    _cb_config_timer.daemon = True
    _cb_config_timer.start()


def _schedule_next_venue_metadata() -> None:
    global _venue_metadata_timer
    if not _running:
        return
    interval = max(get_settings().venues.instrument_refresh_seconds, 60)
    _venue_metadata_timer = threading.Timer(interval, venue_metadata_job)
    _venue_metadata_timer.daemon = True
    _venue_metadata_timer.start()


def _schedule_next_account_snapshot() -> None:
    global _account_snapshot_timer
    if not _running:
        return
    interval = max(get_settings().venues.account_reconcile_seconds, 10)
    _account_snapshot_timer = threading.Timer(interval, account_snapshot_job)
    _account_snapshot_timer.daemon = True
    _account_snapshot_timer.start()


# ---------------------------------------------------------------------------
# 启停接口
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """启动所有定时任务调度器。"""
    global _running
    if not _running:
        _running = True
        _schedule_next()
        _schedule_next_scan_persistence()
        _schedule_next_execution()
        _schedule_next_carry_cost()
        _schedule_next_stats()
        _schedule_next_tradability()
        _schedule_next_session_templates()
        _schedule_next_cb_config()
        _schedule_next_venue_metadata()
        _schedule_next_account_snapshot()
        logger.info("定时任务调度器已启动")


def stop_scheduler() -> None:
    """停止所有定时任务调度器。"""
    global _running
    _running = False
    if _timer:
        _timer.cancel()
    if _stats_timer:
        _stats_timer.cancel()
    if _tradability_timer:
        _tradability_timer.cancel()
    if _session_template_timer:
        _session_template_timer.cancel()
    if _scan_persistence_timer:
        _scan_persistence_timer.cancel()
    if _execution_timer:
        _execution_timer.cancel()
    if _carry_cost_timer:
        _carry_cost_timer.cancel()
    if _cb_config_timer:
        _cb_config_timer.cancel()
    if _venue_metadata_timer:
        _venue_metadata_timer.cancel()
    if _account_snapshot_timer:
        _account_snapshot_timer.cancel()
    logger.info("定时任务调度器已停止")
