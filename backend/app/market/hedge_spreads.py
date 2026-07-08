"""
对冲价差计算模块
==================

根据实时报价缓存计算对冲组（HedgeGroup）的入场价差和平仓价差。

依赖 :mod:`app.market.quotes` 的报价缓存和 :mod:`app.strategy.spread_math`
的价差数学计算函数。
"""

from __future__ import annotations

from typing import Any

from app.adapters.venue import mapping_leg
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import HedgeGroup
from app.market.quotes import quote_cache
from app.strategy.spread_math import spreads_for_direction

logger = get_logger(__name__)


def hedge_group_spreads(group: HedgeGroup, mapping=None) -> dict[str, Any]:
    """计算对冲组的当前入场价差和平仓价差。

    参数:
        group: 对冲组对象，包含 symbol 和 direction 等字段。
        mapping: 品种映射对象（可选），用于确定两腿的 venue。
            为 None 时默认 A 腿为 hyperliquid，B 腿为 mt5。

    返回:
        包含以下字段的字典：
        - ``current_entry_spread``: 当前入场价差
        - ``current_close_spread``: 当前平仓价差
        - ``current_mid_spread``: 当前中间价差
        - ``current_spread_cost``: 当前价差成本
        - ``quote_time_diff_ms``: 两腿报价时间差（毫秒）
        - ``quote_age_ms``: 报价最大年龄（毫秒）
        - 报价缺失时返回 None 值
    """
    if mapping is not None:
        leg_a_venue, _ = mapping_leg(mapping, "a")
        leg_b_venue, _ = mapping_leg(mapping, "b")
    else:
        leg_a_venue, leg_b_venue = "hyperliquid", "mt5"

    hl = quote_cache.latest(leg_a_venue, group.symbol)
    mt5 = quote_cache.latest(leg_b_venue, group.symbol)

    if not hl or not mt5:
        return {
            "current_entry_spread": None,
            "current_close_spread": None,
            "quote_time_diff_ms": None,
            "quote_age_ms": None,
        }

    now = utc_now()
    spreads = spreads_for_direction(group.direction, hl.bid, hl.ask, mt5.bid, mt5.ask)
    return {
        "current_entry_spread": spreads.entry_spread,
        "current_close_spread": spreads.close_spread,
        "current_mid_spread": spreads.mid_spread,
        "current_spread_cost": spreads.spread_cost,
        "quote_time_diff_ms": abs((hl.local_recv_ts - mt5.local_recv_ts).total_seconds() * 1000),
        "quote_age_ms": max(
            (now - hl.local_recv_ts).total_seconds() * 1000,
            (now - mt5.local_recv_ts).total_seconds() * 1000,
        ),
    }
