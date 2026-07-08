"""
自动执行模块
============

定期扫描可执行的套利机会（``ArbitrageOpportunity.status == "executable"``），
经过确认机制（ticks + hold time）、断路器检查和限频控制后，
自动调用 ``open_hedge_group`` 执行开仓。

核心流程：
1. 检查策略配置是否启用自动执行
2. 按净利润降序取前 20 个可执行机会
3. 逐一检查：资格 → 确认 → 断路器 → 决策延迟 → 执行
4. 成功后设置冷却期，失败后恢复机会状态

使用 ``run_worker`` 模板执行，自动记录 WorkerRun 和异常日志。

使用方式::

    from app.core.db_session import db_session
    from app.execution.auto_executor import run_auto_execute

    with db_session() as db:
        run_auto_execute(db)
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.venue import is_native_hyper_mt5_pair
from app.core.logging import get_logger
from app.core.worker_runner import run_worker
from app.db.models import ArbitrageOpportunity, HedgeGroup, StrategySetting, SymbolMapping, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.circuit_breaker import is_blocked as breaker_is_blocked
from app.execution.engine import open_hedge_group
from app.market.mt5_tradability import mt5_tradability_cache

logger = get_logger(__name__)


@dataclass
class OpportunityConfirmation:
    """机会确认状态跟踪。"""
    first_seen: float     # 首次发现时间
    last_seen: float      # 最近发现时间
    ticks: int            # 累计出现次数


# 模块级状态：机会确认跟踪和冷却期
_confirmations: dict[tuple[int, str, str], OpportunityConfirmation] = {}
_cooldown_until: dict[tuple[str, str], float] = {}

# 视为"未平仓"的对冲组状态
OPEN_GROUP_STATUSES = ("opening", "open", "open_partial", "closing", "manual_intervention")


def run_auto_execute(db: Session) -> int:
    """自动执行主入口：扫描并执行符合条件的套利机会。

    参数:
        db: 数据库会话。

    返回:
        成功执行的对冲组数量。
    """
    executed = 0
    strategy = db.query(StrategySetting).first() or StrategySetting()
    if not strategy.auto_execute_enabled:
        return 0
    if strategy.auto_execute_paper_only and strategy.execution_mode != "paper":
        _record_skip(db, "auto_execute 要求 paper 模式，当前执行模式不是 paper")
        return 0
    if not mt5_tradability_cache.initialized():
        _record_skip(db, "MT5 交易能力缓存尚未初始化，暂不自动开仓")
        return 0

    # 按净利润降序取前 20 个可执行机会
    opportunities = (
        db.query(ArbitrageOpportunity)
        .filter(ArbitrageOpportunity.status == "executable")
        .order_by(desc(ArbitrageOpportunity.net_profit))
        .limit(20)
        .all()
    )
    for opportunity in opportunities:
        allowed, reason = _eligible(db, strategy, opportunity)
        if not allowed:
            opportunity.reject_reason = reason
            continue
        confirmed, reason = _confirm(strategy, opportunity)
        if not confirmed:
            opportunity.reject_reason = reason
            continue
        # 断路器检查
        blocked, jitter, threshold = breaker_is_blocked(opportunity.symbol)
        if blocked:
            logger.info(
                "断路器 OPEN，跳过开仓: symbol={} jitter={:.2f} threshold={:.2f}",
                opportunity.symbol, jitter, threshold,
            )
            opportunity.reject_reason = f"断路器 OPEN: jitter={jitter:.3f} threshold={threshold:.3f}"
            continue
        # 决策延迟（模拟人类犹豫时间）
        decision_delay_ms = _decision_delay_ms(strategy)
        if decision_delay_ms > 0:
            time.sleep(decision_delay_ms / 1000)
        try:
            opportunity.status = "executing"
            opportunity.reject_reason = "auto_execute executing"
            db.commit()
            # 执行开仓
            group = open_hedge_group(db, opportunity.id, source="auto_paper")
            _set_cooldown(strategy, opportunity.symbol, opportunity.direction)
            _confirmations.pop(_confirmation_key(opportunity), None)
            if group.status in {"failed", "manual_intervention"}:
                logger.warning("自动纸面执行异常: symbol={} group_id={} status={}", opportunity.symbol, group.id, group.status)
                db.add(SystemLog(level="warning", category="auto_execute", message=f"自动纸面执行异常: {opportunity.symbol} #{group.id}", context=f"group_status={group.status}"))
            else:
                logger.info("自动纸面执行成功: symbol={} group_id={}", opportunity.symbol, group.id)
                db.add(SystemLog(level="info", category="auto_execute", message=f"自动纸面执行成功: {opportunity.symbol} #{group.id}"))
                executed += 1
            prune_table_by_id(db, SystemLog)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("自动执行失败: symbol={}, error={}", opportunity.symbol, exc)
            row = db.get(ArbitrageOpportunity, opportunity.id)
            if row:
                row.status = "executable"
                row.reject_reason = f"自动执行失败: {exc}"
            _set_cooldown(strategy, opportunity.symbol, opportunity.direction)
            db.add(SystemLog(level="warning", category="auto_execute", message=f"自动执行失败: {opportunity.symbol}", context=str(exc)))
            db.add(WorkerRun(worker_name="auto_executor", status="failed", duration_ms=0, error_message=str(exc)))
            prune_table_by_id(db, SystemLog)
            prune_table_by_id(db, WorkerRun)
            db.commit()
    return executed


def run_auto_execute_worker(db: Session) -> int:
    """使用 run_worker 模板包装的自动执行任务（供调度器调用）。"""
    return run_worker(
        db,
        "auto_executor",
        _auto_execute_inner,
        prune_models=[SystemLog, WorkerRun],
    )


def _auto_execute_inner(db: Session) -> int:
    """run_worker 内部执行函数。"""
    return run_auto_execute(db)


def _eligible(db: Session, strategy: StrategySetting, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
    """检查机会是否满足自动执行资格。

    检查项：
    1. 品种映射是否为原生 Hyper/MT5 对（Nautilus 只读 pair 不允许）
    2. 净利润是否达标
    3. 是否在冷却期内
    4. 同品种和全局未平对冲组数量是否超限
    """
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == opportunity.symbol).first()
    if mapping and not is_native_hyper_mt5_pair(mapping):
        return False, "Nautilus V1 只读 venue pair 不允许自动执行"
    min_profit = strategy.auto_execute_min_net_profit or strategy.min_net_profit
    if opportunity.net_profit < min_profit:
        return False, f"自动执行净利润不足: {opportunity.net_profit:.2f} < {min_profit:.2f}"
    # 冷却期检查
    cooldown_key = (opportunity.symbol, opportunity.direction)
    remaining = _cooldown_until.get(cooldown_key, 0.0) - time.time()
    if remaining > 0:
        return False, f"自动执行冷却中: {remaining:.1f}s"
    # 同品种未平对冲组上限
    symbol_open_count = (
        db.query(HedgeGroup)
        .filter(HedgeGroup.symbol == opportunity.symbol, HedgeGroup.status.in_(OPEN_GROUP_STATUSES))
        .count()
    )
    if symbol_open_count >= strategy.auto_execute_max_per_symbol_open_groups:
        return False, f"同品种未平对冲组已达上限: {symbol_open_count}"
    # 全局未平对冲组上限
    global_open_count = db.query(HedgeGroup).filter(HedgeGroup.status.in_(OPEN_GROUP_STATUSES)).count()
    if global_open_count >= strategy.auto_execute_max_global_open_groups:
        return False, f"全局未平对冲组已达上限: {global_open_count}"
    return True, ""


def _confirm(strategy: StrategySetting, opportunity: ArbitrageOpportunity) -> tuple[bool, str]:
    """机会确认：需要满足最小出现次数和持续时间才执行。

    防止短暂的价差异常触发误操作。
    """
    now = time.time()
    key = _confirmation_key(opportunity)
    current = _confirmations.get(key)
    if not current:
        current = OpportunityConfirmation(first_seen=now, last_seen=now, ticks=0)
        _confirmations[key] = current
    current.ticks += 1
    current.last_seen = now
    hold_ms = (now - current.first_seen) * 1000
    required_ticks = max(strategy.auto_execute_confirm_ticks, 1)
    required_hold = max(strategy.auto_execute_min_hold_ms, 0)
    if current.ticks < required_ticks:
        return False, f"自动执行确认次数不足: {current.ticks}/{required_ticks}"
    if hold_ms < required_hold:
        return False, f"自动执行持续时间不足: {hold_ms:.0f}/{required_hold}ms"
    return True, ""


def _confirmation_key(opportunity: ArbitrageOpportunity) -> tuple[int, str, str]:
    """生成机会确认的唯一键。"""
    return (opportunity.id, opportunity.symbol, opportunity.direction)


def _set_cooldown(strategy: StrategySetting, symbol: str, direction: str) -> None:
    """设置品种+方向的冷却期。"""
    _cooldown_until[(symbol, direction)] = time.time() + max(strategy.auto_execute_cooldown_seconds, 0)


def _decision_delay_ms(strategy: StrategySetting) -> int:
    """计算随机决策延迟（模拟人类犹豫）。"""
    low = max(int(strategy.paper_decision_delay_ms_min), 0)
    high = max(int(strategy.paper_decision_delay_ms_max), low)
    return random.randint(low, high)


def _record_skip(db: Session, message: str) -> None:
    """记录自动执行跳过的日志。"""
    db.add(SystemLog(level="warning", category="auto_execute", message=message))
    prune_table_by_id(db, SystemLog)
    db.commit()
