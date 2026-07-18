"""跨 venue 对冲仓位换算。

统一维护 MT5 手数、基础资产数量、另一腿数量和 USD 名义价值之间的换算，
避免调用方把 ``base quantity`` 误当成 ``lot quantity``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from app.market.fx import fx_to_usd


@dataclass(frozen=True)
class PositionSizing:
    """标准化仓位结果；字段名称显式携带数量单位。"""

    leg_b_lots: float
    leg_b_base_quantity: float
    leg_b_point_value_usd: float
    leg_a_base_quantity: float
    notional_usd: float
    currency: str
    fx_rate_to_usd: float
    fx_source: str

    @property
    def leg_b_quantity(self) -> float:
        """兼容旧字段：MT5 腿数量始终表示手数。"""
        return self.leg_b_lots

    @property
    def leg_a_quantity(self) -> float:
        """兼容旧字段：Leg A 数量表示基础资产数量。"""
        return self.leg_a_base_quantity


def calculate_position_sizing(
    mapping,
    *,
    leg_b_mid: float,
    leg_a_mid: float,
    target_notional_usd: float,
) -> PositionSizing:
    """按腿 B 的交易单位计算两腿数量，且不因步进规整突破目标金额。"""
    leg_b_venue = str(getattr(mapping, "leg_b_venue", "mt5") or "mt5").strip().lower()
    if leg_b_venue != "mt5":
        if leg_b_mid <= 0 or leg_a_mid <= 0:
            raise ValueError("报价异常，无法计算名义价值")
        minimum = float(getattr(mapping, "min_order_size", 0.0) or 0.0)
        step = minimum if minimum > 0 else 0.00000001
        requested = float(target_notional_usd or 0.0) / leg_b_mid
        precision = _decimal_places(step)
        quantity = _round_down_to_step(requested, step, precision)
        if quantity < minimum:
            quantity = _round_up_to_step(minimum, step, precision)
        if quantity <= 0:
            raise ValueError("交易所基础资产数量异常")
        notional = leg_b_mid * quantity
        return PositionSizing(
            leg_b_lots=quantity,
            leg_b_base_quantity=quantity,
            leg_b_point_value_usd=quantity,
            leg_a_base_quantity=quantity,
            notional_usd=notional,
            currency=str(getattr(mapping, "quote_asset", "USD") or "USD").upper(),
            fx_rate_to_usd=1.0,
            fx_source="crypto_quote_usd",
        )
    currency = (mapping.mt5_currency_profit or mapping.quote_asset or "USD").upper()
    fx = fx_to_usd(currency)
    contract_size = float(mapping.mt5_contract_size or mapping.contract_multiplier or 1.0)
    lot_min = float(mapping.mt5_min_lot or mapping.min_order_size or 0.0)
    lot_step = float(mapping.mt5_volume_step or lot_min or 0.0)
    if leg_b_mid <= 0 or leg_a_mid <= 0:
        raise ValueError("报价异常，无法计算名义价值")
    lot_notional_usd = leg_b_mid * contract_size * fx.rate_to_usd
    if lot_notional_usd <= 0:
        raise ValueError("MT5 单手 USD 名义价值异常")
    precision = _decimal_places(lot_step)
    minimum_lots = _round_up_to_step(lot_min, lot_step, precision)
    requested_lots = float(target_notional_usd or 0.0) / lot_notional_usd
    if requested_lots <= minimum_lots:
        # 低于交易所最小手数时只能使用最小可交易量；这是唯一允许超过目标金额的情况。
        leg_b_lots = minimum_lots
    else:
        leg_b_lots = max(minimum_lots, _round_down_to_step(requested_lots, lot_step, precision))
    leg_b_base_quantity = leg_b_lots * contract_size
    leg_b_point_value_usd = leg_b_base_quantity * fx.rate_to_usd
    notional_usd = leg_b_mid * leg_b_base_quantity * fx.rate_to_usd
    leg_a_base_quantity = leg_b_point_value_usd
    if leg_a_base_quantity <= 0:
        raise ValueError("Leg A 基础资产数量异常")
    return PositionSizing(
        leg_b_lots=leg_b_lots,
        leg_b_base_quantity=leg_b_base_quantity,
        leg_b_point_value_usd=leg_b_point_value_usd,
        leg_a_base_quantity=round(leg_a_base_quantity, 8),
        notional_usd=notional_usd,
        currency=currency,
        fx_rate_to_usd=fx.rate_to_usd,
        fx_source=fx.source,
    )


def _round_up_to_step(value: float, step: float, precision: int) -> float:
    """向上规整到交易所数量步进。"""
    if step <= 0:
        return round(value, precision)
    decimal_step = Decimal(str(step))
    units = (Decimal(str(value)) / decimal_step).to_integral_value(rounding=ROUND_CEILING)
    return round(float(units * decimal_step), precision)


def _round_down_to_step(value: float, step: float, precision: int) -> float:
    """向下规整到交易所数量步进，避免实际名义价值突破策略目标。"""
    if step <= 0:
        return round(value, precision)
    decimal_step = Decimal(str(step))
    units = (Decimal(str(value)) / decimal_step).to_integral_value(rounding=ROUND_FLOOR)
    return round(float(units * decimal_step), precision)


def _decimal_places(value: float) -> int:
    """返回数量步进的小数位数。"""
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0
