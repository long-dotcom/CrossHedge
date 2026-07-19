"""
断路器模块
==========

实现品种级别的断路器（Circuit Breaker），用于在价差剧烈抖动时自动暂停交易：

- 每个品种维护一个 ``SymbolBreaker`` 实例
- 通过滑动窗口计算价差抖动率（jitter ratio）
- 当抖动率超过自适应阈值时触发断路器（状态变为 OPEN）
- 冷却期结束后自动恢复（状态变回 CLOSED）

核心概念：
- **jitter ratio**：价差变动的交替频率，值越高表示价格来回震荡越剧烈
- **baseline**：5-15 分钟内的抖动率基线（P75 × 倍数 = 自适应阈值）
- **detection window**：滑动检测窗口（默认 5 秒）

使用方式::

    from app.execution.circuit_breaker import feed_spread, is_blocked

    feed_spread("XAUUSD", "long_leg_a_short_leg_b", 0.05)
    blocked, jitter, threshold = is_blocked("XAUUSD")
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TYPE_CHECKING

from app.core.logging import get_logger
from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 模块级实时状态注册表；配置副本保存在 Redis
# ---------------------------------------------------------------------------

# 品种 → 断路器实例的注册表
breaker_registry: dict[str, SymbolBreaker] = {}

_CB_CONFIG_KEY = redis_key("cache", "circuit-breaker-config")


# ---------------------------------------------------------------------------
# 按方向的检测窗口跟踪器
# ---------------------------------------------------------------------------

@dataclass
class _DirectionTracker:
    """持有单个方向的检测窗口数据。"""
    # 滑动窗口：(时间戳, 入场价差) 元组列表
    window: list[tuple[datetime, float]] = field(default_factory=list)
    # 该方向最新的抖动率
    last_jitter: float = 0.0


# ---------------------------------------------------------------------------
# 品种级别断路器
# ---------------------------------------------------------------------------

@dataclass
class SymbolBreaker:
    """品种级别的断路器。

    属性:
        symbol: 品种名称。
        state: 断路器状态，``"CLOSED"``（正常）或 ``"OPEN"``（触发）。
        last_trip_time: 最近一次触发时间。
        cooldown_seconds: 触发后的冷却期（秒）。
        current_jitter_ratio: 当前跨方向最大抖动率。
        threshold: 自适应阈值（baseline P75 × baseline_multiplier）。
    """
    symbol: str
    state: str = "CLOSED"  # CLOSED 或 OPEN
    last_trip_time: datetime | None = None
    cooldown_seconds: float = 3.0

    # 按方向的检测窗口
    _direction_trackers: dict[str, _DirectionTracker] = field(default_factory=dict)

    # 基线抖动率队列（5-15 分钟，每次 update 一个样本）
    baseline_jitters: deque = field(default_factory=lambda: deque(maxlen=2000))

    current_jitter_ratio: float = 0.0
    threshold: float = 0.75  # 初始固定阈值（冷启动时使用）
    baseline_multiplier: float = 2.0  # 基线倍数 K
    min_baseline_samples: int = 50
    detection_seconds: float = 5.0  # 检测窗口大小（秒）

    # -- 配置更新 ----------------------------------------------------------

    def apply_config(self, config: dict[str, Any]) -> None:
        """从配置字典更新断路器参数（来源于 DB）。"""
        if not config:
            return
        self.cooldown_seconds = config.get("cooldown_seconds", self.cooldown_seconds)
        self.threshold = config.get("threshold", self.threshold)
        self.baseline_multiplier = config.get("baseline_multiplier", self.baseline_multiplier)
        self.min_baseline_samples = config.get("min_baseline_samples", self.min_baseline_samples)
        self.detection_seconds = config.get("detection_seconds", self.detection_seconds)

    # -- 公共 API ----------------------------------------------------------

    def update(self, direction: str, entry_spread: float, now: datetime) -> None:
        """每次收到新报价时调用，更新指定方向的检测窗口。

        参数:
            direction: 交易方向。
            entry_spread: 入场价差。
            now: 当前时间戳。
        """
        tracker = self._direction_trackers.setdefault(direction, _DirectionTracker())

        # 追加到该方向的检测窗口
        tracker.window.append((now, entry_spread))
        self._prune_window(tracker.window, now)

        # 计算该方向的抖动率
        tracker.last_jitter = self._calculate_jitter(tracker.window)

        # 跨方向取最大抖动率
        self.current_jitter_ratio = max(
            (t.last_jitter for t in self._direction_trackers.values()),
            default=0.0,
        )

        # 更新基线与自适应阈值
        self._update_baseline()

        # 评估是否触发断路器
        if self.state == "CLOSED" and self.current_jitter_ratio > self.threshold:
            self.state = "OPEN"
            self.last_trip_time = now
            logger.warning(
                "断路器 OPEN: symbol={} jitter={:.3f} threshold={:.3f}",
                self.symbol,
                self.current_jitter_ratio,
                self.threshold,
            )

    def is_blocked(self, now: datetime) -> bool:
        """检查断路器是否处于 OPEN 状态（禁止交易）。

        如果冷却期已结束，自动恢复为 CLOSED 状态。
        """
        if self.state != "OPEN":
            return False
        # 检查冷却期是否结束
        if self.last_trip_time is not None:
            elapsed = (now - self.last_trip_time).total_seconds()
            if elapsed >= self.cooldown_seconds:
                self.state = "CLOSED"
                logger.info(
                    "断路器 CLOSED (冷却结束): symbol={}",
                    self.symbol,
                )
                return False
        return True

    # -- 内部辅助方法 -------------------------------------------------------

    @staticmethod
    def _calculate_jitter(window: list[tuple[datetime, float]]) -> float:
        """计算滑动窗口内价差的抖动率。

        抖动率 = 正负交替次数 / (非零变动次数 - 1)
        值域 [0, 1]，越高表示价格来回震荡越剧烈。
        """
        spreads = [s for _, s in window]
        if len(spreads) < 3:
            return 0.0

        changes = [spreads[i + 1] - spreads[i] for i in range(len(spreads) - 1)]
        # 忽略零变动（价格未变化）
        non_zero = [(i, c) for i, c in enumerate(changes) if abs(c) > 1e-9]
        if len(non_zero) < 2:
            return 0.0

        alternations = 0
        for j in range(1, len(non_zero)):
            prev_sign = 1 if non_zero[j - 1][1] > 0 else -1
            curr_sign = 1 if non_zero[j][1] > 0 else -1
            if prev_sign != curr_sign:
                alternations += 1

        return alternations / (len(non_zero) - 1)

    def _update_baseline(self) -> None:
        """更新基线抖动率和自适应阈值。

        当样本数达到 min_baseline_samples 后，
        使用 P75 × baseline_multiplier 作为新阈值。
        """
        self.baseline_jitters.append(self.current_jitter_ratio)
        if len(self.baseline_jitters) >= self.min_baseline_samples:
            sorted_jitters = sorted(self.baseline_jitters)
            p75_index = int(len(sorted_jitters) * 0.75)
            p75 = sorted_jitters[min(p75_index, len(sorted_jitters) - 1)]
            self.threshold = p75 * self.baseline_multiplier

    def _prune_window(
        self,
        window: list[tuple[datetime, float]],
        now: datetime,
    ) -> None:
        """清理滑动窗口中超出检测时间的旧数据。"""
        cutoff = now.timestamp() - self.detection_seconds
        while window and window[0][0].timestamp() < cutoff:
            window.pop(0)


# ---------------------------------------------------------------------------
# 公共便捷函数
# ---------------------------------------------------------------------------

def _load_cb_settings(db: "Session") -> dict[str, Any]:
    """从 StrategySetting 行加载断路器参数。"""
    from app.db.models import StrategySetting
    row = db.query(StrategySetting).first()
    if not row:
        return {}
    return {
        "cooldown_seconds": getattr(row, "cb_cooldown_seconds", 3.0),
        "threshold": getattr(row, "cb_initial_threshold", 0.75),
        "baseline_multiplier": getattr(row, "cb_baseline_multiplier", 2.0),
        "min_baseline_samples": getattr(row, "cb_min_baseline_samples", 50),
        "detection_seconds": getattr(row, "cb_detection_seconds", 5.0),
    }


def reload_config(db: "Session") -> None:
    """从数据库重新加载断路器配置并推送到所有实例。"""
    config = _load_cb_settings(db)
    redis_client().set(_CB_CONFIG_KEY, json.dumps(config, ensure_ascii=False))
    for breaker in breaker_registry.values():
        breaker.apply_config(config)
    logger.debug("断路器配置已刷新: {}", config)


def _cached_config() -> dict[str, Any]:
    raw = redis_client().get(_CB_CONFIG_KEY)
    return json.loads(raw) if raw else {}


def get_breaker(symbol: str) -> SymbolBreaker:
    """获取或创建指定品种的断路器实例。"""
    if symbol not in breaker_registry:
        breaker_registry[symbol] = SymbolBreaker(symbol=symbol, **_cached_config())
    return breaker_registry[symbol]


def feed_spread(
    symbol: str,
    direction: str,
    entry_spread: float,
    now: datetime | None = None,
) -> None:
    """外部入口：向断路器喂入新的价差数据点。"""
    if now is None:
        now = utc_now()
    get_breaker(symbol).update(direction, entry_spread, now)


def is_blocked(symbol: str, now: datetime | None = None) -> tuple[bool, float, float]:
    """外部入口：检查指定品种是否被断路器阻止。

    返回 ``(blocked, current_jitter, threshold)``。
    """
    if now is None:
        now = utc_now()
    breaker = get_breaker(symbol)
    return breaker.is_blocked(now), breaker.current_jitter_ratio, breaker.threshold


def get_breaker_status(symbol: str) -> dict[str, Any]:
    """返回断路器状态快照（用于日志 / 调试）。"""
    breaker = get_breaker(symbol)
    return {
        "symbol": breaker.symbol,
        "state": breaker.state,
        "current_jitter_ratio": breaker.current_jitter_ratio,
        "threshold": breaker.threshold,
        "baseline_samples": len(breaker.baseline_jitters),
        "last_trip_time": breaker.last_trip_time,
        "cooldown_seconds": breaker.cooldown_seconds,
    }
