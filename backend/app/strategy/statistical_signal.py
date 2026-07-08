"""
统计信号模块
============

基于历史价差分布计算统计信号（分位数、Z-score、过热线等），
为入场/出场决策提供量化依据。

核心函数 ``evaluate_entry_signal`` 根据策略配置（signal_mode）决定使用
简单阈值判定还是统计模型判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean, pstdev
from time import monotonic

from sqlalchemy.orm import Session

from app.analytics.spreads import SpreadPoint, load_spread_points
from app.db.models import StrategySetting, SymbolMapping
from app.strategy.signals import SignalResult, evaluate_signal


@dataclass
class StatisticalSignal:
    """统计信号评估结果。

    属性:
        result: 基础信号判定结果（rejected / candidate / executable）
        reachable_entry: 可达入场线（基于历史分位数或 Z-score）
        cost_guard: 成本保护线（成本分位数，价差需覆盖此线才有边际）
        strong_entry: 强入场线（更高分位数）
        exit_target: 出场目标价差
        overheat: 过热线（极端分位数，超过时需警惕）
        sample_count: 统计样本数
    """
    result: SignalResult
    reachable_entry: float
    cost_guard: float
    strong_entry: float
    exit_target: float
    overheat: float
    sample_count: int


@dataclass(frozen=True)
class SignalStats:
    """统计信号中间计算结果（缓存对象）。"""
    sample_count: int
    reachable_entry: float
    cost_guard: float
    strong_entry: float
    exit_percentile_target: float
    overheat: float


# 信号统计缓存：key → (monotonic_time, SignalStats)
_stats_cache: dict[tuple, tuple[float, SignalStats]] = {}


def evaluate_entry_signal(
    db: Session,
    strategy: StrategySetting,
    symbol: str,
    direction: str,
    current_spread: float,
    unit_cost: float,
    unit_net_profit: float,
    total_net_profit: float,
    annualized_return: float,
) -> StatisticalSignal:
    """评估入场信号。

    当 ``strategy.signal_mode != "statistical"`` 时使用简单阈值判定；
    否则使用基于历史价差分布的统计模型。

    参数:
        db: 数据库会话
        strategy: 策略配置
        symbol: 品种名
        direction: 方向
        current_spread: 当前价差
        unit_cost: 单位成本
        unit_net_profit: 单位净利润
        total_net_profit: 总净利润
        annualized_return: 年化收益率

    返回:
        StatisticalSignal 包含完整信号判定信息
    """
    # 非统计模式：使用简单阈值
    if strategy.signal_mode != "statistical":
        return StatisticalSignal(
            result=evaluate_signal(total_net_profit, annualized_return, strategy.min_net_profit, strategy.min_annualized_return),
            reachable_entry=0.0,
            cost_guard=unit_cost,
            strong_entry=0.0,
            exit_target=0.0,
            overheat=0.0,
            sample_count=0,
        )

    stats = _signal_stats(db, strategy, symbol, direction)
    # 样本不足时仅返回候选状态
    if stats.sample_count < strategy.statistical_min_samples:
        result = SignalResult("candidate", f"统计样本不足 {stats.sample_count}/{strategy.statistical_min_samples}，等待参考数据")
        return StatisticalSignal(result, 0.0, unit_cost, 0.0, 0.0, 0.0, stats.sample_count)

    # 计算带利润缓冲的出场目标
    exit_target = _exit_target_with_profit_buffer(
        percentile_target=stats.exit_percentile_target,
        entry_spread=current_spread,
        unit_cost=stats.cost_guard,
        unit_profit_buffer=_strategy_float(strategy, "auto_close_unit_profit_buffer", 0.0),
    )
    unit_edge = current_spread - stats.cost_guard

    # 多级信号判定
    if current_spread <= stats.cost_guard:
        result = SignalResult("rejected", f"价差 {current_spread:.2f} 未覆盖成本保护线 {stats.cost_guard:.2f}")
    elif current_spread < stats.reachable_entry:
        result = SignalResult("candidate", f"价差 {current_spread:.2f} 未达到可达入场线 {stats.reachable_entry:.2f}")
    elif unit_edge < _strategy_float(strategy, "min_unit_edge", 0.0):
        result = SignalResult("candidate", f"每份边际 {unit_edge:.2f} 低于最小边际 {_strategy_float(strategy, 'min_unit_edge', 0.0):.2f}")
    elif total_net_profit < _strategy_float(strategy, "min_total_profit", 0.0):
        result = SignalResult("candidate", f"总净利润 {total_net_profit:.2f} 低于最小总利润 {_strategy_float(strategy, 'min_total_profit', 0.0):.2f}")
    else:
        result = SignalResult("executable", f"达到可达入场线 {stats.reachable_entry:.2f}，成本保护线 {stats.cost_guard:.2f}")
    return StatisticalSignal(result, stats.reachable_entry, stats.cost_guard, stats.strong_entry, exit_target, stats.overheat, stats.sample_count)


def clear_signal_stats_cache() -> None:
    """清空信号统计缓存。"""
    _stats_cache.clear()


def refresh_signal_stats_cache(db: Session) -> int:
    """主动刷新所有品种的信号统计缓存。

    返回刷新的 (品种×方向) 数量。
    """
    strategy = db.query(StrategySetting).first() or StrategySetting()
    if strategy.signal_mode != "statistical":
        return 0
    refreshed = 0
    symbols = [row.symbol for row in db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()]
    now = monotonic()
    for symbol in symbols:
        for direction in ("long_leg_a_short_leg_b", "long_leg_b_short_leg_a"):
            entry_points, close_points = _load_entry_and_close_points(db, symbol, direction, strategy.statistical_lookback_range)
            stats = _compute_signal_stats(entry_points, close_points, strategy)
            _stats_cache[_stats_cache_key(db, strategy, symbol, direction)] = (now, stats)
            refreshed += 1
    return refreshed


def _signal_stats(db: Session, strategy: StrategySetting, symbol: str, direction: str) -> SignalStats:
    """获取信号统计结果（优先读缓存，缓存未命中则计算）。"""
    key = _stats_cache_key(db, strategy, symbol, direction)
    cached = _stats_cache.get(key)
    if cached:
        return cached[1]
    entry_points, close_points = _load_entry_and_close_points(db, symbol, direction, strategy.statistical_lookback_range)
    stats = _compute_signal_stats(entry_points, close_points, strategy)
    _stats_cache[key] = (monotonic(), stats)
    return stats


def _stats_cache_key(db: Session, strategy: StrategySetting, symbol: str, direction: str) -> tuple:
    """构造缓存键，包含所有影响计算结果的参数。"""
    return (
        id(db.get_bind()),
        symbol.upper(),
        direction,
        strategy.statistical_lookback_range,
        strategy.statistical_min_samples,
        _strategy_float(strategy, "reachable_entry_percentile", 0.75),
        _strategy_float(strategy, "reachable_entry_zscore", 1.0),
        _strategy_float(strategy, "cost_guard_percentile", 0.90),
        _strategy_float(strategy, "exit_target_percentile", 0.25),
    )


def _load_entry_and_close_points(db: Session, symbol: str, direction: str, range_value: str) -> tuple[list[SpreadPoint], list[SpreadPoint]]:
    """加载入场和收盘价差历史数据点。

    兼容 ``load_spread_points`` 签名变更（basis 参数可选）。
    """
    try:
        entry_points = load_spread_points(db, symbol, direction, range_value, basis="entry")
        close_points = load_spread_points(db, symbol, direction, range_value, basis="close")
        return entry_points, close_points
    except TypeError:
        points = load_spread_points(db, symbol, direction, range_value)
        return points, points


def _compute_signal_stats(entry_points: list[SpreadPoint], close_points: list[SpreadPoint], strategy: StrategySetting) -> SignalStats:
    """从历史数据点计算统计信号。

    计算逻辑:
    - reachable_entry: 取分位数和 mean+zscore 的较大值
    - cost_guard: 成本分布的高分位数（保守估计）
    - strong_entry: 90 分位和 mean+1.5σ 的较大值
    - exit_target: 收盘价差低分位数
    - overheat: 99 分位数（极端值警戒）
    """
    sample_count = len(entry_points)
    if not entry_points:
        return SignalStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
    spreads = [point.spread for point in entry_points]
    close_spreads = [point.spread for point in close_points] or spreads
    costs = [point.total_cost for point in entry_points]
    avg = mean(spreads)
    std = pstdev(spreads) if sample_count > 1 else 0.0
    reachable_entry = max(
        _percentile(spreads, _strategy_float(strategy, "reachable_entry_percentile", 0.75)),
        avg + _strategy_float(strategy, "reachable_entry_zscore", 1.0) * std,
    )
    cost_guard = _percentile(costs, _strategy_float(strategy, "cost_guard_percentile", 0.90))
    strong_entry = max(_percentile(spreads, 0.90), avg + 1.5 * std)
    exit_percentile_target = _percentile(close_spreads, _strategy_float(strategy, "exit_target_percentile", 0.25))
    overheat = _percentile(spreads, 0.99)
    return SignalStats(sample_count, reachable_entry, cost_guard, strong_entry, exit_percentile_target, overheat)


def _strategy_float(strategy: StrategySetting, name: str, default: float) -> float:
    """安全读取策略配置的浮点属性。"""
    value = getattr(strategy, name, None)
    return default if value is None else float(value)


def _percentile(values: list[float], percentile: float) -> float:
    """线性插值分位数计算。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    pct = min(max(percentile, 0.0), 1.0)
    index = pct * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _exit_target_with_profit_buffer(
    *,
    percentile_target: float,
    entry_spread: float,
    unit_cost: float,
    unit_profit_buffer: float,
) -> float:
    """计算带利润缓冲的出场目标。

    出场目标 = min(分位数目标, 入场价差 - 成本 - 缓冲)，确保出场时仍有利润。
    """
    profit_safe_target = entry_spread - max(unit_cost, 0.0) - max(unit_profit_buffer, 0.0)
    if percentile_target <= 0 or profit_safe_target <= 0:
        return 0.0
    return min(percentile_target, profit_safe_target)
