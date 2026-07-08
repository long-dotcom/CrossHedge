"""
价差数学计算模块
================

定义价差方向常量和双向价差计算逻辑。
所有方向均使用规范化常量 ``LONG_LEG_A_SHORT_LEG_B`` / ``LONG_LEG_B_SHORT_LEG_A``。
"""

from __future__ import annotations

from dataclasses import dataclass


# ── 规范化方向常量 ──────────────────────────────────────────────────────────
LONG_LEG_A_SHORT_LEG_B = "long_leg_a_short_leg_b"
LONG_LEG_B_SHORT_LEG_A = "long_leg_b_short_leg_a"
DIRECTIONS = (LONG_LEG_A_SHORT_LEG_B, LONG_LEG_B_SHORT_LEG_A)

# ── 已废弃的别名（向后兼容） ────────────────────────────────────────────────
LONG_HL_SHORT_MT5 = LONG_LEG_A_SHORT_LEG_B  # deprecated
LONG_MT5_SHORT_HL = LONG_LEG_B_SHORT_LEG_A  # deprecated


@dataclass(frozen=True)
class DirectionSpreads:
    """单方向价差结果。

    属性:
        direction: 方向标识（``LONG_LEG_A_SHORT_LEG_B`` 或 ``LONG_LEG_B_SHORT_LEG_A``）
        entry_spread: 入场价差（买入 leg_b 卖出 leg_a 或反向）
        close_spread: 平仓价差（反向操作的价格）
        mid_spread: 中间价差（两腿中间价之差）
        spread_cost: 价差成本（close - entry，表示来回摩擦）
    """
    direction: str
    entry_spread: float
    close_spread: float
    mid_spread: float
    spread_cost: float

    @property
    def gross_spread(self) -> float:
        """毛价差，等价于 entry_spread（向后兼容别名）。"""
        return self.entry_spread


def spreads_for_direction(
    direction: str,
    leg_a_bid: float,
    leg_a_ask: float,
    leg_b_bid: float,
    leg_b_ask: float,
) -> DirectionSpreads:
    """根据方向计算入场/平仓/中间价差。

    - ``LONG_LEG_A_SHORT_LEG_B``：买入 leg_a（ask）、卖出 leg_b（bid）
      → entry = leg_b_bid - leg_a_ask
    - ``LONG_LEG_B_SHORT_LEG_A``：卖出 leg_a（bid）、买入 leg_b（ask）
      → entry = leg_a_bid - leg_b_ask

    参数:
        direction: 价差方向常量
        leg_a_bid/leg_a_ask: A 腿买/卖价
        leg_b_bid/leg_b_ask: B 腿买/卖价

    返回:
        DirectionSpreads 包含各维度价差

    异常:
        ValueError: 方向不在 DIRECTIONS 中时抛出
    """
    leg_a_mid = (leg_a_bid + leg_a_ask) / 2
    leg_b_mid = (leg_b_bid + leg_b_ask) / 2
    if direction == LONG_LEG_A_SHORT_LEG_B:
        entry_spread = leg_b_bid - leg_a_ask
        close_spread = leg_b_ask - leg_a_bid
        mid_spread = leg_b_mid - leg_a_mid
    elif direction == LONG_LEG_B_SHORT_LEG_A:
        entry_spread = leg_a_bid - leg_b_ask
        close_spread = leg_a_ask - leg_b_bid
        mid_spread = leg_a_mid - leg_b_mid
    else:
        raise ValueError(f"未知价差方向: {direction}")
    return DirectionSpreads(
        direction=direction,
        entry_spread=entry_spread,
        close_spread=close_spread,
        mid_spread=mid_spread,
        spread_cost=close_spread - entry_spread,
    )
