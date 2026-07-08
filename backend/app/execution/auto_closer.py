"""
自动平仓模块
============

定期评估所有活跃对冲组是否满足平仓条件：
- 平仓价差回归至退出线以下
- 超过最大持仓时间且利润达标
- 无退出线时价差回到零轴

核心流程：
1. 遍历内存池中的活跃对冲组
2. 逐一评估 ``evaluate_auto_close``
3. 检查断路器状态
4. 执行平仓 ``close_hedge_group_from_pool``

使用 ``run_worker`` 模板执行，自动记录 WorkerRun 和异常日志。

使用方式::

    from app.core.db_session import db_session
    from app.execution.auto_closer import run_auto_close

    with db_session() as db:
        run_auto_close(db)
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import StrategySetting, SystemLog, WorkerRun
from app.db.retention import prune_table_by_id
from app.execution.circuit_breaker import is_blocked as breaker_is_blocked
from app.execution.engine import (
    _close_sides,
    _execution_adapters,
    _has_position_effect,
    _is_pending_result,
    _paper_latency_ms,
    _paper_live_parallel_enabled,
    _platform_close_quantity,
)
from app.execution.gateway import LegOrderIntent, build_execution_gateway
from app.execution.hedge_pool import (
    CloseFillSnapshot,
    CloseOrderSnapshot,
    CloseResultEvent,
    HedgeGroupSnapshot,
    hedge_pool,
)
from app.execution.pnl import pnl_from_close_spread
from app.market.active_refresh import refresh_execution_quotes
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.market.quotes import quote_synchronizer
from app.market.scanner import get_strategy_setting
from app.market.symbols import enabled_mappings
from app.strategy.spread_math import spreads_for_direction

logger = get_logger(__name__)


@dataclass(frozen=True)
class CloseEvaluation:
    """平仓评估结果。"""
    should_close: bool
    reason: str
    close_spread: float
    exit_target: float
    estimated_profit: float


def run_auto_close(db: Session) -> int:
    """自动平仓主入口：遍历活跃对冲组并评估是否平仓。

    参数:
        db: 数据库会话。

    返回:
        成功平仓的对冲组数量。
    """
    closed = 0
    strategy = get_strategy_setting(db)
    if not strategy.auto_close_enabled:
        return 0

    modes = ["paper"]
    if strategy.auto_close_live_enabled:
        modes.append("live")
    mappings = {mapping.symbol: mapping for mapping in enabled_mappings(db)}
    for group in hedge_pool.snapshot_open_groups(modes):
        try:
            if group.execution_mode == "live":
                _log(db, "warning", f"跳过 live 自动平仓: {group.symbol} #{group.id}", "内存化自动平仓首版仅执行 paper 路径")
                continue
            mapping = mappings.get(group.symbol)
            if not mapping:
                _log(db, "warning", f"自动平仓跳过: {group.symbol} #{group.id}", "品种映射不在运行缓存中")
                continue
            evaluation = evaluate_auto_close(db, strategy, group, mapping=mapping)
            if not evaluation.should_close:
                hedge_pool.upsert_group(group.with_updates(unrealized_pnl=evaluation.estimated_profit))
                continue
            # 断路器检查
            blocked, jitter, threshold = breaker_is_blocked(group.symbol)
            if blocked:
                logger.info(
                    "断路器 OPEN，跳过平仓: symbol={} jitter={:.2f} threshold={:.2f}",
                    group.symbol, jitter, threshold,
                )
                hedge_pool.upsert_group(group.with_updates(unrealized_pnl=evaluation.estimated_profit))
                continue
            if close_hedge_group_from_pool(db, group.id, evaluation.reason, evaluation=evaluation, mapping=mapping, strategy=strategy, auto=True):
                closed += 1
        except Exception as exc:
            db.rollback()
            logger.exception("自动平仓检查失败: symbol={}, group_id={}, error={}", group.symbol, group.id, exc)
            _log(db, "warning", f"自动平仓检查失败: {group.symbol} #{group.id}", str(exc))
    return closed


def run_auto_close_worker(db: Session) -> int:
    """使用 run_worker 模板包装的自动平仓任务（供调度器调用）。"""
    return run_worker(
        db,
        "auto_closer",
        _auto_close_inner,
        prune_models=[WorkerRun, SystemLog],
    )


def _auto_close_inner(db: Session) -> int:
    """run_worker 内部执行函数。"""
    return run_auto_close(db)


def close_hedge_group_from_pool(
    db: Session,
    group_id: int,
    reason: str,
    *,
    evaluation: CloseEvaluation | None = None,
    mapping: SimpleNamespace | None = None,
    strategy: SimpleNamespace | None = None,
    auto: bool = False,
    force_strategy_checks: bool = False,
) -> HedgeGroupSnapshot:
    """从内存池执行对冲组平仓。

    参数:
        db: 数据库会话。
        group_id: 对冲组 ID。
        reason: 平仓原因。
        evaluation: 预计算的平仓评估（可选）。
        mapping: 品种映射（可选）。
        strategy: 策略配置（可选）。
        auto: 是否为自动平仓（影响事件类型前缀）。
        force_strategy_checks: 是否强制执行策略检查。
    """
    snapshot = hedge_pool.get(group_id)
    if not snapshot:
        raise ValueError("对冲组不在运行池中")
    if snapshot.execution_mode != "paper":
        raise ValueError("内存化平仓首版仅支持 paper 对冲组")
    if snapshot.status not in {"open", "open_partial"}:
        raise ValueError("当前状态不允许平仓")
    strategy = strategy or get_strategy_setting(db)
    if mapping is None:
        mappings = {item.symbol: item for item in enabled_mappings(db)}
        mapping = mappings.get(snapshot.symbol)
    if not mapping:
        raise ValueError("品种映射不在运行缓存中")
    if evaluation is None:
        evaluation = evaluate_auto_close(db, strategy, snapshot, mapping=mapping, force=force_strategy_checks)
    if not evaluation.should_close:
        raise ValueError(evaluation.reason)
    closing = hedge_pool.try_mark_closing(snapshot.id, reason, evaluation.estimated_profit)
    if not closing:
        raise ValueError("对冲组已经在平仓中")
    try:
        result = _execute_paper_close_snapshot(db, closing, mapping, strategy, reason, evaluation, auto=auto, enforce_strategy_close_checks=not force_strategy_checks)
        hedge_pool.enqueue_close_result(result)
        if result.status == "closed":
            closed = hedge_pool.mark_closed(closing.id, realized_pnl=result.realized_pnl, fees_delta=result.fees_delta, reason=result.close_reason, status="closed")
            _log(db, "info", f"{'自动' if auto else '手工'}纸面平仓成功: {closing.symbol} #{closing.id}", result.event_detail)
            return closed or closing
        if result.status == "manual_intervention":
            manual = hedge_pool.mark_manual_intervention(closing.id, result.close_reason)
            _log(db, "warning", f"{'自动' if auto else '手工'}平仓单边异常: {closing.symbol} #{closing.id}", result.event_detail)
            return manual or closing
        hedge_pool.upsert_group(closing.with_updates(status=result.status, close_reason=result.close_reason, unrealized_pnl=result.unrealized_pnl or closing.unrealized_pnl))
        _log(db, "info", f"{'自动' if auto else '手工'}纸面平仓已提交: {closing.symbol} #{closing.id}", result.event_detail)
        return hedge_pool.get(closing.id) or closing
    except Exception as exc:
        hedge_pool.restore_status(snapshot, reason=str(exc))
        raise


def evaluate_auto_close(
    db: Session,
    strategy: StrategySetting | SimpleNamespace,
    group: HedgeGroupSnapshot,
    *,
    mapping: SimpleNamespace | None = None,
    force: bool = False,
) -> CloseEvaluation:
    """评估对冲组是否满足平仓条件。

    评估逻辑：
    1. 同步报价（strict 模式），失败则尝试主动刷新
    2. 计算平仓价差和估算利润
    3. 根据退出线、最大持仓时间等条件判断是否平仓

    参数:
        db: 数据库会话。
        strategy: 策略配置。
        group: 对冲组快照。
        mapping: 品种映射（可选）。
        force: 强制平仓（跳过利润检查）。
    """
    if not isinstance(group, HedgeGroupSnapshot):
        group = HedgeGroupSnapshot.from_row(group)
    if mapping is None:
        mapping = next((item for item in enabled_mappings(db) if item.symbol == group.symbol), None)
    settings = get_settings()
    synced, sync_reason = quote_synchronizer.synchronized(
        group.symbol,
        mode="strict",
        max_time_diff_ms=settings.quote.strict_sync_ms,
        max_age_ms=settings.quote.stale_ms,
    )
    refreshed: list[str] = []
    if not synced and mapping is not None:
        refreshed = refresh_execution_quotes(mapping)
        if refreshed:
            synced, sync_reason = quote_synchronizer.synchronized(
                group.symbol,
                mode="strict",
                max_time_diff_ms=settings.quote.strict_sync_ms,
                max_age_ms=settings.quote.stale_ms,
            )
    if not synced:
        suffix = f"；执行前主动刷新: {','.join(refreshed)}" if refreshed else ""
        return CloseEvaluation(False, f"{sync_reason}{suffix}", 0.0, group.exit_target or 0.0, group.unrealized_pnl)

    close_spread = spreads_for_direction(group.direction, synced.leg_a.bid, synced.leg_a.ask, synced.leg_b.bid, synced.leg_b.ask).close_spread
    exit_target = _effective_exit_target(group, mapping)
    estimated_profit = pnl_from_close_spread(group, close_spread)
    min_profit = float(strategy.auto_close_min_profit or 0.0)
    hold_expired = _hold_expired(group, strategy)

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


def _execute_paper_close_snapshot(
    db: Session,
    group: HedgeGroupSnapshot,
    mapping: SimpleNamespace,
    strategy: SimpleNamespace,
    reason: str,
    evaluation: CloseEvaluation,
    *,
    auto: bool,
    enforce_strategy_close_checks: bool = True,
) -> CloseResultEvent:
    """执行 Paper 模式平仓，返回平仓结果事件。"""
    # MT5 会话检查
    session_state = mt5_session_state(mapping)
    mt5_close_allowed, mt5_close_reason = mt5_action_allowed(session_state, group.direction, "close")
    if not mt5_close_allowed:
        raise ValueError(mt5_close_reason)
    # 最终复核
    if enforce_strategy_close_checks:
        final_ok, final_reason = _final_close_still_executable_snapshot(group, mapping, strategy, evaluation.close_spread, evaluation.exit_target, evaluation.estimated_profit)
        if not final_ok:
            raise ValueError(final_reason)

    hl_side, mt5_side = _close_sides(group.direction)
    leg_a_adapter, leg_b_adapter = _execution_adapters(live=False, simulated=True, mapping=mapping, db=db)
    leg_a_quantity = _platform_close_quantity(group.leg_a_quantity, group.quantity)
    leg_b_quantity = _platform_close_quantity(group.leg_b_quantity, group.quantity)
    if leg_a_quantity <= 0 and leg_b_quantity <= 0:
        raise ValueError("对冲组没有可平仓数量")

    results = []
    if leg_a_quantity > 0 and leg_b_quantity > 0 and _paper_live_parallel_enabled(live=False, simulated=True, hl=leg_a_adapter, mapping=mapping, db=db):
        results = _submit_parallel_close(group, mapping, leg_a_adapter, leg_b_adapter, hl_side, mt5_side, leg_a_quantity, leg_b_quantity, strategy)
    elif leg_a_quantity > 0:
        hl_result = _submit_close_leg(group, mapping, leg_a_adapter, mapping.leg_a_venue, hl_side, leg_a_quantity, mapping.hl_close_order_type, mapping.leg_a_venue_symbol, strategy)
        results.append(hl_result)
        if _has_position_effect(hl_result.adapter_result) and leg_b_quantity > 0:
            fill_ratio = hl_result.adapter_result.filled_quantity / leg_a_quantity if leg_a_quantity > 0 else 0.0
            results.append(_submit_close_leg(group, mapping, leg_b_adapter, mapping.leg_b_venue, mt5_side, leg_b_quantity * fill_ratio, mapping.mt5_close_order_type, mapping.mt5_symbol, strategy))
    elif leg_b_quantity > 0:
        results.append(_submit_close_leg(group, mapping, leg_b_adapter, mapping.leg_b_venue, mt5_side, leg_b_quantity, mapping.mt5_close_order_type, mapping.mt5_symbol, strategy))

    order_snapshots = tuple(_order_snapshot(item) for item in results)
    adapter_results = [item.adapter_result for item in results]
    fees_delta = sum(float(result.fee or 0.0) for result in adapter_results)
    event_prefix = "auto_" if auto else ""
    if all(_has_position_effect(result) for result in adapter_results):
        realized = _realized_pnl_from_close_fills(group, order_snapshots)
        if realized is None:
            realized = evaluation.estimated_profit
        closed_at = utc_now()
        return CloseResultEvent(group.id, "closed", reason, f"{event_prefix}closed", reason, realized, 0.0, fees_delta, closed_at, order_snapshots)
    if any(_has_position_effect(result) for result in adapter_results):
        detail = f"平仓单边成交: {reason}"
        return CloseResultEvent(group.id, "manual_intervention", detail, "manual_intervention", detail, None, evaluation.estimated_profit, fees_delta, None, order_snapshots)
    if any(_is_pending_result(result) for result in adapter_results):
        detail = f"平仓订单待成交: {reason}"
        return CloseResultEvent(group.id, "closing", detail, f"{event_prefix}close_pending", detail, None, evaluation.estimated_profit, fees_delta, None, order_snapshots)
    detail = f"平仓失败: {reason}"
    return CloseResultEvent(group.id, "open", detail, f"{event_prefix}close_failed", detail, None, evaluation.estimated_profit, fees_delta, None, order_snapshots)


def _submit_parallel_close(group, mapping, leg_a_adapter, leg_b_adapter, hl_side, mt5_side, leg_a_quantity, leg_b_quantity, strategy):
    """并行提交双边平仓订单。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_submit_close_leg, group, mapping, leg_a_adapter, mapping.leg_a_venue, hl_side, leg_a_quantity, mapping.hl_close_order_type, mapping.leg_a_venue_symbol, strategy),
            pool.submit(_submit_close_leg, group, mapping, leg_b_adapter, mapping.leg_b_venue, mt5_side, leg_b_quantity, mapping.mt5_close_order_type, mapping.mt5_symbol, strategy),
        ]
        return [future.result() for future in futures]


def _submit_close_leg(group, mapping, adapter, platform: str, side: str, quantity: float, order_type: str, venue_symbol: str, strategy):
    """提交单腿平仓订单。"""
    if platform == mapping.leg_a_venue and getattr(adapter, "simulated", False):
        refresh_execution_quotes(mapping, refresh_mt5=False)
    gateway = build_execution_gateway(adapter)
    return gateway.submit_order(
        LegOrderIntent(
            platform=platform,
            symbol=group.symbol,
            side=side,
            quantity=quantity,
            venue_symbol=venue_symbol,
            order_type=order_type,
            reduce_only=True,
            hedge_group_id=group.id,
        ),
        paper_latency_ms=_paper_latency_ms(strategy, platform, adapter),
    )


def _order_snapshot(gateway_result) -> CloseOrderSnapshot:
    """从网关结果构建平仓订单快照。"""
    result = gateway_result.adapter_result
    fills = tuple(
        CloseFillSnapshot(
            platform=fill.platform,
            symbol=fill.symbol,
            side=fill.side,
            quantity=float(fill.quantity or 0.0),
            price=float(fill.price or 0.0),
            fee=float(fill.fee or 0.0),
            external_order_id=str(fill.external_order_id or ""),
        )
        for fill in gateway_result.fill_events
    )
    event = gateway_result.order_event
    return CloseOrderSnapshot(
        platform=event.platform,
        symbol=event.symbol,
        side=event.side,
        quantity=float(event.requested_quantity or result.requested_quantity or 0.0),
        order_type="market",
        price=None,
        post_only=False,
        reduce_only=True,
        ttl_seconds=0,
        status=str(result.status or event.status),
        external_order_id=str(result.external_order_id or event.external_order_id or ""),
        average_price=result.average_price or event.average_price,
        error_message=str(result.error_message or event.message or ""),
        filled_quantity=float(result.filled_quantity or event.filled_quantity or 0.0),
        fee=float(result.fee or event.fee or 0.0),
        fills=fills,
    )


def _realized_pnl_from_close_fills(group: HedgeGroupSnapshot, orders: tuple[CloseOrderSnapshot, ...]) -> float | None:
    """根据平仓成交记录计算已实现盈亏。"""
    prices = {order.platform: order.average_price for order in orders if order.average_price}
    leg_a_venue = getattr(group, "_leg_a_venue", None)
    leg_b_venue = getattr(group, "_leg_b_venue", None)
    if leg_a_venue and leg_b_venue:
        if leg_a_venue not in prices or leg_b_venue not in prices:
            return None
    elif "hyperliquid" not in prices or "mt5" not in prices:
        return None
    if group.direction == "long_leg_a_short_leg_b":
        close_spread = float(prices.get(leg_b_venue or "mt5")) - float(prices.get(leg_a_venue or "hyperliquid"))
    else:
        close_spread = float(prices.get(leg_a_venue or "hyperliquid")) - float(prices.get(leg_b_venue or "mt5"))
    return pnl_from_close_spread(group, close_spread)


def _final_close_still_executable_snapshot(group, mapping, strategy, close_spread: float, exit_target: float, estimated_profit: float) -> tuple[bool, str]:
    """平仓前最终复核：检查退出线和最低利润是否仍满足。"""
    hold_expired = _hold_expired(group, strategy)
    if exit_target != 0 and close_spread > exit_target and not hold_expired:
        return False, f"自动平仓最终复核失败: 平仓价差 {close_spread:.6f} > 退出线 {exit_target:.6f}"
    min_profit = float(strategy.auto_close_min_profit or 0.0)
    if estimated_profit < min_profit:
        return False, f"自动平仓最终复核失败: 估算平仓利润 {estimated_profit:.2f} < {min_profit:.2f}"
    return True, ""


def _effective_exit_target(group: HedgeGroupSnapshot, mapping: SimpleNamespace | None) -> float:
    """计算有效退出线（取对冲组和品种映射中的较小值）。"""
    group_target = float(group.exit_target or 0.0)
    mapping_target = float(getattr(mapping, "max_close_spread", 0.0) or 0.0) if mapping else 0.0
    if group_target and mapping_target:
        return min(group_target, mapping_target)
    return group_target or mapping_target


def _hold_expired(group: HedgeGroupSnapshot, strategy: StrategySetting | SimpleNamespace) -> bool:
    """判断是否超过最大持仓时间。"""
    if not group.opened_at:
        return False
    return utc_now() - group.opened_at >= timedelta(minutes=max(int(strategy.max_holding_minutes or 1), 1))


def _log(db: Session, level: str, message: str, context: str = "") -> None:
    """写入 SystemLog 并立即提交。"""
    db.add(SystemLog(level=level, category="auto_close", message=message, context=context))
    prune_table_by_id(db, SystemLog)
    db.commit()
