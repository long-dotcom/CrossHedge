"""
盈亏计算模块
============

提供对冲组的入场价差、平仓价差和已实现盈亏的计算逻辑：
- 根据 Fill 记录计算加权平均成交价
- 根据方向（long_leg_a_short_leg_b 等）计算入场/平仓价差
- 根据平仓价差估算已实现盈亏

使用方式::

    from app.execution.pnl import actual_entry_spread_from_fills, pnl_from_close_spread

    entry = actual_entry_spread_from_fills(db, group)
    profit = pnl_from_close_spread(group, close_spread)
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.type_utils import safe_float
from app.db.models import ExecutionIntent, ExecutionLeg, Fill, HedgeGroup, Order, SymbolMapping, VenueOrder


def actual_entry_spread_from_fills(
    db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None,
) -> float | None:
    """根据开仓 Fill 记录计算实际入场价差。"""
    return actual_spread_from_fills(db, group, reduce_only=False, mapping=mapping)


def actual_close_spread_from_fills(
    db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None,
) -> float | None:
    """根据平仓 Fill 记录计算实际平仓价差。"""
    return actual_spread_from_fills(db, group, reduce_only=True, mapping=mapping)


def actual_spread_from_fills(
    db: Session,
    group: HedgeGroup,
    *,
    reduce_only: bool,
    mapping: SymbolMapping | None = None,
) -> float | None:
    """根据 Fill 记录计算加权价差。

    参数:
        db: 数据库会话。
        group: 对冲组。
        reduce_only: True 表示仅统计平仓订单的 Fill，False 表示仅统计开仓订单。
        mapping: 品种映射（可选，不提供则从数据库查询）。

    返回:
        加权价差，或 None（任一腿缺少成交记录时）。
    """
    if mapping is None:
        mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    action = "CLOSE" if reduce_only else "OPEN"
    leg_a_price = weighted_execution_price(db, group.id, leg_a_venue, action=action)
    leg_b_price = weighted_execution_price(db, group.id, leg_b_venue, action=action)
    # 兼容重构前仅写入 orders/fills 的历史成交。
    if leg_a_price is None:
        leg_a_price = weighted_fill_price(db, group.id, leg_a_venue, reduce_only=reduce_only)
    if leg_b_price is None:
        leg_b_price = weighted_fill_price(db, group.id, leg_b_venue, reduce_only=reduce_only)
    if leg_a_price is None or leg_b_price is None:
        return None
    # long_leg_a_short_leg_b: 价差 = leg_b 价格 - leg_a 价格
    if group.direction == "long_leg_a_short_leg_b":
        return leg_b_price - leg_a_price
    return leg_a_price - leg_b_price


def pnl_from_close_spread(group: HedgeGroup, close_spread: float) -> float:
    """根据平仓价差估算已实现盈亏。

    公式：(入场价差 - 平仓价差) × 数量 - 开平仓交易手续费
    """
    quantity = safe_float(group.leg_a_quantity or group.quantity, 1.0)
    entry_spread = safe_float(group.entry_spread or group.entry_threshold)
    gross = (entry_spread - close_spread) * quantity
    return gross - safe_float(group.fees)


def realized_pnl_from_fills(
    db: Session, group: HedgeGroup, *, mapping: SymbolMapping | None = None,
) -> float | None:
    """根据平仓 Fill 记录计算已实现盈亏。"""
    close_spread = actual_close_spread_from_fills(db, group, mapping=mapping)
    if close_spread is None:
        return None
    return pnl_from_close_spread(group, close_spread)


def weighted_fill_price(
    db: Session, group_id: int, platform: str, *, reduce_only: bool,
) -> float | None:
    """计算指定平台、指定方向（开仓/平仓）的加权平均成交价。

    返回:
        加权平均价，或 None（无成交记录时）。
    """
    rows = (
        db.query(Fill)
        .join(Order, Fill.order_id == Order.id)
        .filter(
            Order.hedge_group_id == group_id,
            Order.platform == platform,
            Order.reduce_only.is_(reduce_only),
            Fill.quantity > 0,
            Fill.price > 0,
        )
        .all()
    )
    quantity = sum(safe_float(row.quantity) for row in rows)
    if quantity <= 0:
        return None
    notional = sum(safe_float(row.quantity) * safe_float(row.price) for row in rows)
    return notional / quantity


def weighted_execution_price(
    db: Session, group_id: int, venue: str, *, action: str,
) -> float | None:
    """按新执行模型中的交易所回报计算指定腿的加权成交均价。

    ``VenueOrder.average_price`` 和 ``filled_quantity`` 是订单回报/成交事件投影后的
    权威字段。Maker 拆单或补单产生多条订单时，按各订单累计成交量加权。
    """
    rows = (
        db.query(VenueOrder)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .join(ExecutionIntent, ExecutionIntent.id == ExecutionLeg.intent_id)
        .filter(
            ExecutionIntent.hedge_group_id == group_id,
            ExecutionLeg.venue == str(venue or "").lower(),
            ExecutionLeg.action == action.upper(),
            VenueOrder.filled_quantity > 0,
            VenueOrder.average_price.is_not(None),
            VenueOrder.average_price > 0,
        )
        .all()
    )
    quantity = sum(safe_float(row.filled_quantity) for row in rows)
    if quantity <= 0:
        return None
    notional = sum(safe_float(row.filled_quantity) * safe_float(row.average_price) for row in rows)
    return notional / quantity
