"""自动平仓评估与异步 Intent 创建。

本模块只负责判断是否应平仓并创建持久化 ``CLOSE`` Intent。任何交易所命令
都必须由 Outbox Worker 执行；调度线程和 HTTP 请求线程不得直接下单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import HedgeGroup, StrategySetting, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.circuit_breaker import is_blocked as breaker_is_blocked
from app.execution.coordinator import create_close_intent
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.pnl import pnl_from_close_spread
from app.market.active_refresh import refresh_execution_quotes
from app.market.quotes import quote_synchronizer
from app.market.scanner import get_strategy_setting
from app.market.symbols import enabled_mappings
from app.strategy.live_costs import VenueCostUnavailable, estimated_pair_close_fee
from app.strategy.spread_math import spreads_for_direction

logger = get_logger(__name__)


@dataclass(frozen=True)
class CloseEvaluation:
    """一次自动平仓评估结果。"""

    should_close: bool
    reason: str
    close_spread: float
    exit_target: float
    estimated_profit: float


def run_auto_close(db: Session) -> int:
    """评估活动组并创建异步 ``CLOSE`` Intent。"""
    created_count = 0
    strategy = get_strategy_setting(db)
    if not strategy.auto_close_enabled:
        return 0

    modes = ["paper"]
    if strategy.auto_close_live_enabled:
        modes.append("live")
    mappings = {mapping.symbol: mapping for mapping in enabled_mappings(db)}
    for snapshot in hedge_pool.snapshot_open_groups(modes):
        try:
            # 数据库是权威状态；Redis 快照池只用于候选遍历。
            group = db.get(HedgeGroup, snapshot.id)
            if group is None or group.status not in {"open", "open_partial"}:
                continue
            mapping = mappings.get(group.symbol)
            if mapping is None:
                _log(db, "warning", f"自动平仓跳过: {group.symbol} #{group.id}", "品种映射不在运行缓存中")
                continue
            evaluation = evaluate_auto_close(db, strategy, group, mapping=mapping)
            if not evaluation.should_close:
                group.unrealized_pnl = evaluation.estimated_profit
                db.flush()
                hedge_pool.upsert_group(group)
                continue
            blocked, jitter, threshold = breaker_is_blocked(group.symbol)
            if blocked:
                logger.info(
                    "断路器 OPEN，跳过平仓: symbol={} jitter={:.2f} threshold={:.2f}",
                    group.symbol,
                    jitter,
                    threshold,
                )
                group.unrealized_pnl = evaluation.estimated_profit
                db.flush()
                hedge_pool.upsert_group(group)
                continue
            result = create_close_intent(
                db,
                group_id=group.id,
                reason=evaluation.reason,
                requested_by="auto_closer",
                # 同一分钟的调度重试复用同一请求，避免并发重复 Intent。
                idempotency_key=f"auto-close:{group.id}:{utc_now().strftime('%Y%m%d%H%M')}",
            )
            db.commit()
            persisted = db.get(HedgeGroup, group.id)
            if persisted is not None:
                hedge_pool.upsert_group(persisted)
            if result.created:
                created_count += 1
        except Exception as exc:
            db.rollback()
            logger.exception("自动平仓检查失败: symbol={}, group_id={}, error={}", snapshot.symbol, snapshot.id, exc)
            _log(db, "warning", f"自动平仓检查失败: {snapshot.symbol} #{snapshot.id}", str(exc))
    return created_count


def run_auto_close_worker(db: Session) -> int:
    """使用统一 Worker 模板执行自动平仓评估。"""
    return run_worker(
        db,
        "auto_closer",
        lambda session: run_auto_close(session),
        prune_models=[WorkerRun, SystemLog],
    )


def evaluate_auto_close(
    db: Session,
    strategy: StrategySetting | SimpleNamespace,
    group: HedgeGroupSnapshot | HedgeGroup,
    *,
    mapping: SimpleNamespace | None = None,
    force: bool = False,
) -> CloseEvaluation:
    """基于同步报价、退出线、持仓时长和最低利润评估平仓。"""
    snapshot = group if isinstance(group, HedgeGroupSnapshot) else HedgeGroupSnapshot.from_row(group)
    if mapping is None:
        mapping = next((item for item in enabled_mappings(db) if item.symbol == snapshot.symbol), None)
    settings = get_settings()
    synced, sync_reason = quote_synchronizer.synchronized(
        snapshot.symbol,
        mode="strict",
        max_time_diff_ms=settings.quote.strict_sync_ms,
        max_age_ms=settings.quote.stale_ms,
    )
    refreshed: list[str] = []
    if not synced and mapping is not None:
        refreshed = refresh_execution_quotes(mapping)
        if refreshed:
            synced, sync_reason = quote_synchronizer.synchronized(
                snapshot.symbol,
                mode="strict",
                max_time_diff_ms=settings.quote.strict_sync_ms,
                max_age_ms=settings.quote.stale_ms,
            )
    if not synced:
        suffix = f"；执行前主动刷新: {','.join(refreshed)}" if refreshed else ""
        return CloseEvaluation(False, f"{sync_reason}{suffix}", 0.0, snapshot.exit_target or 0.0, snapshot.unrealized_pnl)

    close_spread = spreads_for_direction(
        snapshot.direction,
        synced.leg_a.bid,
        synced.leg_a.ask,
        synced.leg_b.bid,
        synced.leg_b.ask,
    ).close_spread
    exit_target = _effective_exit_target(snapshot, mapping)
    estimated_profit = pnl_from_close_spread(snapshot, close_spread)
    try:
        estimated_profit -= estimated_pair_close_fee(mapping, snapshot.notional) if mapping else 0.0
    except VenueCostUnavailable as exc:
        if not force:
            return CloseEvaluation(False, f"自动平仓成本不可用: {exc}", close_spread, exit_target, estimated_profit)
        logger.warning("强制平仓无法估算 close fee，继续执行: group_id={}, error={}", snapshot.id, exc)

    min_profit = float(strategy.auto_close_min_profit or 0.0)
    hold_expired = _hold_expired(snapshot, strategy, mapping)
    if force:
        return CloseEvaluation(True, f"手工强制平仓: 估算利润 {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
    if estimated_profit < min_profit:
        return CloseEvaluation(False, f"估算平仓利润不足: {estimated_profit:.2f} < {min_profit:.2f}", close_spread, exit_target, estimated_profit)
    if exit_target <= 0:
        if close_spread <= 0:
            return CloseEvaluation(True, f"无统计退出线但平仓价差已回到零轴: {close_spread:.2f} <= 0.00", close_spread, exit_target, estimated_profit)
        if hold_expired:
            return CloseEvaluation(True, f"缺少退出线但超过最大持仓时间且利润达标: {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
        return CloseEvaluation(False, "缺少退出线，等待更多统计样本", close_spread, exit_target, estimated_profit)
    if close_spread <= exit_target:
        return CloseEvaluation(True, f"平仓价差回归至退出线: {close_spread:.2f} <= {exit_target:.2f}", close_spread, exit_target, estimated_profit)
    if hold_expired:
        return CloseEvaluation(True, f"超过最大持仓时间且利润达标: {estimated_profit:.2f}", close_spread, exit_target, estimated_profit)
    return CloseEvaluation(False, f"等待平仓价差回归: {close_spread:.2f} > {exit_target:.2f}", close_spread, exit_target, estimated_profit)


def _effective_exit_target(group: HedgeGroupSnapshot, mapping: SimpleNamespace | None) -> float:
    """取对冲组与品种映射中更严格的正退出线。"""
    group_target = float(group.exit_target or 0.0)
    mapping_target = float(getattr(mapping, "max_close_spread", 0.0) or 0.0) if mapping else 0.0
    if group_target and mapping_target:
        return min(group_target, mapping_target)
    return group_target or mapping_target


def _hold_expired(
    group: HedgeGroupSnapshot,
    strategy: StrategySetting | SimpleNamespace,
    mapping: SimpleNamespace | None = None,
) -> bool:
    """判断是否超过品种级或策略级最大持仓时间。"""
    if not group.opened_at:
        return False
    minutes = getattr(mapping, "max_holding_minutes", None) if mapping else None
    if minutes is None:
        minutes = getattr(strategy, "max_holding_minutes", 1)
    return utc_now() - group.opened_at >= timedelta(minutes=max(int(minutes or 1), 1))


def _log(db: Session, level: str, message: str, context: str = "") -> None:
    """写入自动平仓日志。"""
    db.add(SystemLog(level=level, category="auto_close", message=message, context=context))
    prune_table_by_id(db, SystemLog)
    db.commit()
