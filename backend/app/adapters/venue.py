"""品种映射工具与原生连接器兼容入口。"""

from __future__ import annotations

from app.venues.manager import native_venue_manager

SUPPORTED_VENUES = {"hyperliquid", "mt5", "binance"}
# 保留旧常量名，调用方含义已统一为项目原生支持集合。
NATIVE_VENUES = SUPPORTED_VENUES


def normalize_venue(value: str) -> str:
    return (value or "").strip().lower()


def build_market_adapter(venue: str, *, live: bool = False):
    """兼容旧调用名，返回统一原生或 Paper 连接器。"""
    normalized = normalize_venue(venue)
    if normalized not in SUPPORTED_VENUES:
        raise ValueError(f"尚未接入原生交易场所: {normalized}")
    return native_venue_manager.connector_for(normalized, "live" if live else "paper")


def is_native_pair(mapping) -> bool:
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_b_venue, _ = mapping_leg(mapping, "b")
    return leg_a_venue in SUPPORTED_VENUES and leg_b_venue in SUPPORTED_VENUES


is_native_hyper_mt5_pair = is_native_pair


def mapping_leg(mapping, index: str) -> tuple[str, str]:
    """从品种映射中提取指定腿的交易所和交易所品种。"""
    if index == "a":
        venue = normalize_venue(getattr(mapping, "leg_a_venue", "")) or "hyperliquid"
        symbol = str(
            getattr(mapping, "leg_a_symbol", "")
            or getattr(mapping, "leg_a_venue_symbol", "")
            or getattr(mapping, "symbol", "")
        )
        return venue, symbol
    venue = normalize_venue(getattr(mapping, "leg_b_venue", "")) or "mt5"
    symbol = str(
        getattr(mapping, "leg_b_symbol", "")
        or getattr(mapping, "mt5_symbol", "")
        or getattr(mapping, "symbol", "")
    )
    return venue, symbol


def configured_venues_from_mappings(mappings) -> list[str]:
    venues: list[str] = []
    for mapping in mappings:
        for index in ("a", "b"):
            venue, _ = mapping_leg(mapping, index)
            if venue in SUPPORTED_VENUES and venue not in venues:
                venues.append(venue)
    return venues
