"""与交易所无关的双腿开仓模式配置。"""

from __future__ import annotations


SIMULTANEOUS_MARKET = "simultaneous_market"
MAKER_THEN_MARKET = "maker_then_market"


def execution_mode(mapping) -> str:
    """读取执行模式，并兼容历史配置值。"""
    value = str(getattr(mapping, "execution_style", "") or "").lower()
    if value == "hyper_maker_mt5_taker":
        return MAKER_THEN_MARKET
    if value == "taker_taker":
        return SIMULTANEOUS_MARKET
    return value or SIMULTANEOUS_MARKET


def maker_leg(mapping) -> str:
    """返回 Maker 腿；历史 Maker-Taker 配置默认 A 腿。"""
    value = str(getattr(mapping, "maker_leg", "a") or "a").lower()
    return value if value in {"a", "b"} else "a"


def other_leg(leg: str) -> str:
    return "b" if leg == "a" else "a"


def open_order_type(mapping, leg: str) -> str:
    """执行模式决定开仓订单类型，不再依赖 venue 名称。"""
    if execution_mode(mapping) == SIMULTANEOUS_MARKET:
        return "market"
    return "limit" if leg == maker_leg(mapping) else "market"


def close_order_type(mapping, leg: str) -> str:
    """平仓与开仓使用同一执行模式。"""
    return open_order_type(mapping, leg)


def maker_offset_bps(mapping) -> float:
    return float(getattr(mapping, "maker_offset_bps", None) or getattr(mapping, "hl_maker_offset_bps", 1.0) or 0.0)


def maker_ttl_seconds(mapping) -> int:
    value = getattr(mapping, "maker_order_ttl_seconds", None)
    return max(int(value if value is not None else getattr(mapping, "hl_order_ttl_seconds", 3) or 0), 0)


def maker_unfilled_action(mapping) -> str:
    value = str(getattr(mapping, "maker_unfilled_action", "") or "").lower()
    if not value:
        value = str(getattr(mapping, "hl_unfilled_action", "cancel") or "cancel").lower()
    return "market_fallback" if value in {"market_fallback", "taker_fallback"} else "cancel"
