"""
适配器工厂与品种映射工具
=========================

提供交易所适配器（Adapter）的工厂方法和品种映射辅助函数：

- :func:`build_market_adapter` —— 根据 venue 名称构建对应的适配器实例
- :func:`is_native_pair` —— 判断品种映射是否为可直接执行的原生配对（Hyperliquid + MT5）
- :func:`mapping_leg` —— 从品种映射中提取指定腿的 venue 和品种名
- :func:`nautilus_venues_from_mappings` —— 从品种映射列表中提取 Nautilus venue
"""

from __future__ import annotations

from app.adapters.base import ExchangeAdapter
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.adapters.nautilus import NautilusAdapter
from app.core.logging import get_logger

logger = get_logger(__name__)

# 原生适配器 venue 集合（不需要通过 Nautilus 连接）
NATIVE_VENUES = {"hyperliquid", "mt5"}


def normalize_venue(value: str) -> str:
    """标准化 venue 名称（去除空白并转小写）。"""
    return (value or "").strip().lower()


def build_market_adapter(venue: str, *, live: bool = False) -> ExchangeAdapter:
    """根据 venue 名称构建对应的交易所适配器。

    参数:
        venue: 交易所名称（如 ``"hyperliquid"`` / ``"mt5"`` / ``"binance"``）。
        live: 是否启用实盘模式。

    返回:
        对应的适配器实例：
        - ``"hyperliquid"`` → :class:`HyperliquidAdapter`
        - ``"mt5"`` → :class:`MT5Adapter`
        - 其他 → :class:`NautilusAdapter`
    """
    venue = normalize_venue(venue)
    if venue == "hyperliquid":
        return HyperliquidAdapter(live=live)
    if venue == "mt5":
        return MT5Adapter(live=live)
    return NautilusAdapter(venue, live=live)


def is_native_pair(mapping) -> bool:
    """判断品种映射是否为可直接执行的原生配对。

    原生配对定义：A 腿为 Hyperliquid，B 腿为 MT5。
    这类配对可以直接通过内置适配器执行，无需 Nautilus。
    """
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_b_venue, _ = mapping_leg(mapping, "b")
    return leg_a_venue == "hyperliquid" and leg_b_venue == "mt5"


# 向后兼容别名
is_native_hyper_mt5_pair = is_native_pair


def mapping_leg(mapping, index: str) -> tuple[str, str]:
    """从品种映射中提取指定腿的 venue 和品种名。

    参数:
        mapping: 品种映射对象（SymbolMapping 或 SimpleNamespace）。
        index: 腿标识，``"a"`` 或 ``"b"``。

    返回:
        ``(venue, symbol)`` 元组。A 腿默认为 hyperliquid，B 腿默认为 mt5。
    """
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


def nautilus_venues_from_mappings(mappings) -> list[str]:
    """从品种映射列表中提取需要 Nautilus 连接的 venue 列表。

    排除原生 venue（hyperliquid / mt5），仅返回需要通过 Nautilus 连接的交易所。

    参数:
        mappings: 品种映射对象列表。

    返回:
        去重后的 Nautilus venue 名称列表。
    """
    venues: list[str] = []
    for mapping in mappings:
        for index in ("a", "b"):
            venue, _ = mapping_leg(mapping, index)
            if venue in NATIVE_VENUES or venue in venues:
                continue
            venues.append(venue)
    return venues
