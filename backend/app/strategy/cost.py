"""
交易成本估算模块
================

提供 ``CostBreakdown`` 数据类和 ``estimate_cost`` 函数，
用于估算跨交易所对冲策略的完整交易成本，包括：

- Leg A（Hyperliquid）手续费、点差、funding
- Leg B（MT5）手续费、点差、隔夜利息
- 滑点成本、外汇转换成本

使用方式::

    from app.strategy.cost import estimate_cost

    cost = estimate_cost(notional=10000, ...)
    print(cost.total)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostBreakdown:
    """交易成本明细。

    包含两腿各项成本分量及汇总属性 ``total``。
    """
    leg_a_fee: float          # Leg A（Hyperliquid）开/平仓手续费
    leg_a_spread: float       # Leg A 买卖点差成本
    leg_a_funding: float      # Leg A 资金费率成本（持仓收益/支出）
    leg_b_spread: float       # Leg B（MT5）买卖点差成本
    leg_b_commission: float   # Leg B 佣金
    leg_b_swap: float         # Leg B 隔夜利息
    slippage: float           # 滑点成本
    fx_cost: float            # 外汇转换成本
    leg_a_fee_rate: float = 0.00045       # Leg A 手续费率（记录用）
    leg_a_funding_rate: float = 0.00010   # Leg A 资金费率（记录用）
    leg_b_commission_rate: float = 0.00035  # Leg B 佣金率（记录用）
    source: str = "static"    # 成本数据来源标识

    # ── 向后兼容别名 ─────────────────────────────────────────────────────────
    @property
    def hyperliquid_fee(self) -> float:
        return self.leg_a_fee

    @property
    def hyperliquid_spread(self) -> float:
        return self.leg_a_spread

    @property
    def hyperliquid_funding(self) -> float:
        return self.leg_a_funding

    @property
    def mt5_spread(self) -> float:
        return self.leg_b_spread

    @property
    def mt5_commission(self) -> float:
        return self.leg_b_commission

    @property
    def mt5_swap(self) -> float:
        return self.leg_b_swap

    @property
    def total(self) -> float:
        """所有成本分量之和。"""
        return (
            self.leg_a_fee
            + self.leg_a_spread
            + self.leg_a_funding
            + self.leg_b_spread
            + self.leg_b_commission
            + self.leg_b_swap
            + self.slippage
            + self.fx_cost
        )


def estimate_cost(
    notional: float,
    leg_b_bid: float,
    leg_b_ask: float,
    max_slippage_bps: float,
    quantity: float = 0.0,
    leg_a_bid: float = 0.0,
    leg_a_ask: float = 0.0,
    leg_a_fee_rate: float = 0.00045,
    leg_a_fee_round_trips: float = 2.0,
    leg_a_close_fee_rate: float | None = None,
    leg_a_funding_rate: float = 0.00010,
    leg_a_side: str = "buy",
    leg_b_commission_rate: float = 0.00035,
    leg_b_swap_cost: float | None = None,
    holding_hours: float = 4.0,
    leg_b_spread_rebate_rate: float = 0.0,
    fx_cost_rate: float = 0.0,
    source: str = "static",
) -> CostBreakdown:
    """估算完整交易成本。

    参数:
        notional: 名义价值（USD）
        leg_b_bid/leg_b_ask: Leg B 买/卖价
        max_slippage_bps: 最大滑点（基点）
        quantity: Leg A 数量（用于计算点差成本）
        leg_a_bid/leg_a_ask: Leg A 买/卖价
        leg_a_fee_rate: Leg A 开仓手续费率
        leg_a_fee_round_trips: 手续费往返倍数（当 close_fee_rate 为 None 时使用）
        leg_a_close_fee_rate: Leg A 平仓手续费率（None 时按 round_trips 估算）
        leg_a_funding_rate: Leg A 资金费率
        leg_a_side: Leg A 方向（``"buy"`` / ``"sell"``），决定 funding 方向
        leg_b_commission_rate: Leg B 佣金率
        leg_b_swap_cost: Leg B 隔夜利息（None 时按 0 计）
        holding_hours: 预估持仓时间（小时）
        leg_b_spread_rebate_rate: Leg B 点差返佣比例
        fx_cost_rate: 外汇转换成本费率
        source: 成本数据来源标识

    返回:
        CostBreakdown 包含各成本分量
    """
    # Leg B 点差成本 = |ask - bid| / mid × 名义价值 × (1 - 返佣率)
    leg_b_spread_cost = (
        abs(leg_b_ask - leg_b_bid)
        / max((leg_b_ask + leg_b_bid) / 2, 1)
        * notional
        * (1 - leg_b_spread_rebate_rate)
    )
    # Hyperliquid funding 是持仓收益/成本，正 funding 通常多头支付、空头收取
    funding_direction = 1 if leg_a_side == "buy" else -1
    # Leg A 点差成本 = max(ask - bid, 0) × 数量
    leg_a_spread_cost = max(leg_a_ask - leg_a_bid, 0) * quantity if quantity > 0 else 0.0
    return CostBreakdown(
        leg_a_fee=notional * _fee_multiplier(leg_a_fee_rate, leg_a_close_fee_rate, leg_a_fee_round_trips),
        leg_a_spread=leg_a_spread_cost,
        leg_a_funding=notional * leg_a_funding_rate * max(holding_hours, 0) * funding_direction,
        leg_b_spread=leg_b_spread_cost,
        leg_b_commission=notional * leg_b_commission_rate,
        leg_b_swap=leg_b_swap_cost if leg_b_swap_cost is not None else 0.0,
        slippage=notional * max_slippage_bps / 10_000,
        fx_cost=notional * fx_cost_rate,
        leg_a_fee_rate=leg_a_fee_rate,
        leg_a_funding_rate=leg_a_funding_rate,
        leg_b_commission_rate=leg_b_commission_rate,
        source=source,
    )


def _fee_multiplier(open_fee_rate: float, close_fee_rate: float | None, round_trips: float) -> float:
    """计算手续费乘数。

    如果指定了平仓手续费率，直接相加；否则按往返倍数估算。
    """
    if close_fee_rate is not None:
        return open_fee_rate + close_fee_rate
    return open_fee_rate * round_trips
