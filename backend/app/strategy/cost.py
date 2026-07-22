"""
交易成本估算模块
================

扫描与候选阶段只计算两类成本：

- 可成交 bid/ask 已体现的双腿点差；
- 两条腿的开仓、平仓交易手续费。

Funding、MT5 Swap、预测滑点和 FX 附加成本不进入机会判断。真实持仓已经发生的
Funding/Swap 仍由执行账本单独记录，不属于本模块的预测成本。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostBreakdown:
    """手续费与点差成本明细。"""

    leg_a_fee: float
    leg_a_spread: float
    leg_b_spread: float
    leg_b_fee: float
    leg_a_fee_rate: float = 0.0
    leg_b_fee_rate: float = 0.0
    source: str = "static"

    @property
    def total(self) -> float:
        """预测总成本只包含点差和交易手续费。"""
        return self.leg_a_fee + self.leg_a_spread + self.leg_b_spread + self.leg_b_fee

    @property
    def leg_b_commission(self) -> float:
        """兼容旧字段名。"""
        return self.leg_b_fee

    @property
    def hyperliquid_fee(self) -> float:
        return self.leg_a_fee

    @property
    def hyperliquid_spread(self) -> float:
        return self.leg_a_spread

    @property
    def mt5_spread(self) -> float:
        return self.leg_b_spread

    @property
    def mt5_commission(self) -> float:
        return self.leg_b_fee

    def as_dict(self) -> dict[str, float | str]:
        return {
            "leg_a_fee": self.leg_a_fee,
            "leg_a_spread": self.leg_a_spread,
            "leg_b_spread": self.leg_b_spread,
            "leg_b_fee": self.leg_b_fee,
            "total": self.total,
            "source": self.source,
        }


def estimate_pair_cost(
    *,
    notional: float,
    leg_a_open_fee_rate: float,
    leg_a_close_fee_rate: float,
    leg_b_open_fee_rate: float,
    leg_b_close_fee_rate: float,
    source: str = "venue",
) -> CostBreakdown:
    """计算双腿开平仓手续费。

    扫描器的入场价差和平仓目标均由可成交 bid/ask 计算，点差已经包含在两者差值
    中，因此这里不再重复增加点差成本。
    """
    value = max(float(notional or 0.0), 0.0)
    leg_a_rate = float(leg_a_open_fee_rate or 0.0) + float(leg_a_close_fee_rate or 0.0)
    leg_b_rate = float(leg_b_open_fee_rate or 0.0) + float(leg_b_close_fee_rate or 0.0)
    return CostBreakdown(
        leg_a_fee=value * leg_a_rate,
        leg_a_spread=0.0,
        leg_b_spread=0.0,
        leg_b_fee=value * leg_b_rate,
        leg_a_fee_rate=float(leg_a_open_fee_rate or 0.0),
        leg_b_fee_rate=float(leg_b_open_fee_rate or 0.0),
        source=source,
    )


def estimate_cost(
    notional: float,
    leg_b_bid: float,
    leg_b_ask: float,
    quantity: float = 0.0,
    leg_a_bid: float = 0.0,
    leg_a_ask: float = 0.0,
    leg_a_fee_rate: float = 0.00045,
    leg_a_fee_round_trips: float = 2.0,
    leg_a_close_fee_rate: float | None = None,
    leg_b_fee_rate: float = 0.00035,
    leg_b_spread_rebate_rate: float = 0.0,
    source: str = "static",
) -> CostBreakdown:
    """兼容分析调用的点差与手续费成本计算。"""
    value = max(float(notional or 0.0), 0.0)
    leg_b_spread = (
        abs(float(leg_b_ask) - float(leg_b_bid))
        / max((float(leg_b_ask) + float(leg_b_bid)) / 2, 1.0)
        * value
        * (1 - float(leg_b_spread_rebate_rate or 0.0))
    )
    leg_a_spread = max(float(leg_a_ask) - float(leg_a_bid), 0.0) * max(float(quantity or 0.0), 0.0)
    leg_a_rate = (
        float(leg_a_fee_rate or 0.0) + float(leg_a_close_fee_rate or 0.0)
        if leg_a_close_fee_rate is not None
        else float(leg_a_fee_rate or 0.0) * float(leg_a_fee_round_trips or 0.0)
    )
    return CostBreakdown(
        leg_a_fee=value * leg_a_rate,
        leg_a_spread=leg_a_spread,
        leg_b_spread=leg_b_spread,
        leg_b_fee=value * float(leg_b_fee_rate or 0.0),
        leg_a_fee_rate=float(leg_a_fee_rate or 0.0),
        leg_b_fee_rate=float(leg_b_fee_rate or 0.0),
        source=source,
    )
