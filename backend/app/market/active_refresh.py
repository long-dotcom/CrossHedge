"""
执行前行情刷新模块
==================

在执行下单前刷新两腿报价：

- Leg A（Hyperliquid）：通过 l2Book HTTP 接口复核
- Leg B（MT5）：通过 symbol_info_tick 刷新

使用 ``post_hyperliquid_info`` 统一 HTTP 调用，
MT5 报价通过 Redis Gateway 代理读取。
"""

from __future__ import annotations

from app.config.settings import get_settings
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.market.orderbook import order_book_cache, parse_l2_levels
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
    if _refresh_leg_a_quote(mapping):
        refreshed.append("leg_a")
    if refresh_leg_b and _refresh_leg_b_quote(mapping):
        refreshed.append("leg_b")
    return refreshed


def _refresh_leg_a_quote(mapping) -> bool:
    """刷新 Leg A 报价。

    常规行情走 WS；执行前只做一次 l2Book HTTP 复核，避免额外请求触发 429。
    """
    return _refresh_leg_a_l2book(mapping)


def _refresh_leg_a_l2book(mapping) -> bool:
    """通过 Hyperliquid l2Book 接口刷新 Leg A 报价。"""
    settings = get_settings()
    try:
        data = post_hyperliquid_info(
            settings.hyperliquid.info_url,
            {"type": "l2Book", "coin": mapping.leg_a_venue_symbol},
            timeout=3.0,
        )
        levels = data.get("levels") if isinstance(data, dict) else data
        _write_leg_a_levels(mapping.symbol, levels, "hyperliquid_http_l2Book_execution_refresh")
        return True
    except Exception:
        return False


def _write_leg_a_levels(symbol: str, levels, source: str) -> None:
    """将 l2Book 数据写入报价缓存。"""
    bids, asks = parse_l2_levels(levels)
    if not bids or not asks:
        raise ValueError("l2Book levels 为空")
    bid, bid_size = bids[0]
    ask, ask_size = asks[0]
    depth_notional = min(bid * bid_size, ask * ask_size)
    order_book_cache.put("hyperliquid", symbol, bids, asks, source)
    quote_cache.put("hyperliquid", symbol, bid, ask, depth_notional, source)


def _refresh_leg_b_quote(mapping) -> bool:
    """通过独立 MT5 Gateway 刷新 Leg B 报价。"""
    try:
        connector = native_venue_manager.connector_for("mt5", "live")
        tick = connector.get_ticker(mapping.mt5_symbol)
        depth = min(tick.bid * tick.bid_quantity, tick.ask * tick.ask_quantity)
        quote_cache.put(
            "mt5", mapping.symbol, float(tick.bid), float(tick.ask), float(depth),
            "mt5_gateway_execution_refresh", tick.exchange_time,
        )
        return True
    except Exception:
        return False
