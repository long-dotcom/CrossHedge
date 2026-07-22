"""
执行前行情刷新模块
==================

在执行下单前按品种映射刷新两腿 BBO 报价，不获取或维护完整订单簿。
"""

from __future__ import annotations

from app.adapters.venue import mapping_leg
from app.core.logging import get_logger
from app.market.quotes import quote_cache
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)


def refresh_execution_quotes(
    mapping,
    *,
    refresh_leg_b: bool = True,
    refresh_mt5: bool | None = None,
) -> list[str]:
    """刷新执行前的报价。

    参数:
        mapping: 品种映射对象
        refresh_leg_b: 是否刷新 Leg B（MT5）报价
        refresh_mt5: 已废弃的 refresh_leg_b 别名

    返回:
        已刷新的腿名列表，如 ``["leg_a", "leg_b"]``
    """
    # refresh_mt5 是 refresh_leg_b 的已废弃别名
    if refresh_mt5 is not None:
        refresh_leg_b = refresh_mt5
    refreshed: list[str] = []
    if _refresh_leg_quote(mapping, "a"):
        refreshed.append("leg_a")
    if refresh_leg_b and _refresh_leg_quote(mapping, "b"):
        refreshed.append("leg_b")
    return refreshed


def _refresh_leg_quote(mapping, leg: str) -> bool:
    """通过实际交易所连接器刷新指定腿的 BBO 报价。"""
    venue, venue_symbol = mapping_leg(mapping, leg)
    try:
        connector = native_venue_manager.connector_for(venue, "live")
        ticker = connector.get_ticker(venue_symbol)
        bid_depth_notional = float(ticker.bid * ticker.bid_quantity)
        ask_depth_notional = float(ticker.ask * ticker.ask_quantity)
        depth_notional = min(bid_depth_notional, ask_depth_notional)
        quote_cache.put(
            venue,
            mapping.symbol,
            float(ticker.bid),
            float(ticker.ask),
            max(depth_notional, 0.0),
            f"{venue}_execution_bbo_refresh",
            ticker.exchange_time,
            local_recv_ts=ticker.received_at,
            bid_depth_notional=max(bid_depth_notional, 0.0),
            ask_depth_notional=max(ask_depth_notional, 0.0),
        )
        return True
    except Exception as exc:
        logger.warning(
            "执行前 BBO 刷新失败: mapping={}, leg={}, venue={}, symbol={}, error={}",
            mapping.symbol, leg, venue, venue_symbol, exc,
        )
        return False
