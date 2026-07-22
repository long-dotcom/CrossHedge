"""
实时成本数据模块
================

按 venue 自动获取实时交易手续费率，包括：

- Hyperliquid 用户费率（userFees）
- Hyperliquid 资产元数据对应的费率倍率（metaAndAssetCtxs）
- 原生连接器返回的 maker/taker 费率

使用 ``TTLCache`` 缓存结果，避免频繁请求。

使用方式::

    from app.strategy.live_costs import leg_a_cost_inputs

    hl_costs = leg_a_cost_inputs("ETH")
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.settings import get_settings
from app.core.cache import TTLCache
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.core.type_utils import safe_float
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)


@dataclass
class VenueCostInputs:
    """交易所（Venue）成本输入参数。

    属性:
        taker_fee_rate: Taker 费率
        maker_fee_rate: Maker 费率
        source: 数据来源标识
    """
    taker_fee_rate: float
    maker_fee_rate: float
    source: str


# 向后兼容别名
HyperliquidCostInputs = VenueCostInputs


@dataclass
class HyperliquidMarketData:
    """用于确定 HIP-3 费率倍率的资产元数据。"""
    asset_meta: dict[str, dict]


# ── TTL 缓存实例 ────────────────────────────────────────────────────────────
# 市场数据缓存（按 dex 分组），TTL 由 settings.cost.cost_cache_ttl_seconds 控制
_cost_cache_ttl = max(float(get_settings().cost.cost_cache_ttl_seconds), 1.0)
_hl_market_cache: TTLCache[HyperliquidMarketData] = TTLCache(ttl_seconds=_cost_cache_ttl, namespace="hl-market-costs")
# 用户费率缓存，TTL 同上
_hl_user_fee_cache: TTLCache[tuple[float, float]] = TTLCache(ttl_seconds=_cost_cache_ttl, namespace="hl-user-fees")
# 交易所手续费缓存，避免扫描循环反复请求品种费率。
_venue_cost_cache: TTLCache[VenueCostInputs] = TTLCache(ttl_seconds=_cost_cache_ttl, namespace="venue-costs")


class VenueCostUnavailable(RuntimeError):
    """venue 交易手续费率无法自动获取。"""


def venue_cost_inputs(venue: str, symbol: str) -> VenueCostInputs:
    """按 venue 自动获取 maker/taker 交易手续费率。

    不支持或查询失败时抛出异常，调用方必须阻止成本未知的候选进入执行，
    禁止静默套用 Hyperliquid 费率或按零成本处理。
    """
    normalized = str(venue or "").strip().lower()
    if normalized == "hyperliquid":
        return leg_a_cost_inputs(symbol)
    if normalized == "mt5":
        return VenueCostInputs(0.0, 0.0, "mt5_no_trading_fee")

    cache_key = f"{normalized}:{str(symbol or '').upper()}"
    cached = _venue_cost_cache.get(cache_key)
    if cached:
        return cached
    try:
        from app.venues.manager import native_venue_manager

        instrument = native_venue_manager.connector_for(normalized, "live").get_instrument(symbol)
        result = VenueCostInputs(
            taker_fee_rate=safe_float(instrument.taker_fee_rate),
            maker_fee_rate=safe_float(instrument.maker_fee_rate),
            source=f"native_{normalized}",
        )
    except Exception as exc:
        raise VenueCostUnavailable(f"{normalized} {symbol} 自动成本查询失败: {exc}") from exc
    _venue_cost_cache.set(cache_key, result)
    return result


def estimated_pair_close_fee(mapping, notional: float) -> float:
    """按平仓执行模式估算两腿手续费。"""
    from app.adapters.venue import mapping_leg
    from app.execution.modes import MAKER_THEN_MARKET, execution_mode, maker_leg

    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
    leg_a = venue_cost_inputs(leg_a_venue, leg_a_symbol)
    leg_b = venue_cost_inputs(leg_b_venue, leg_b_symbol)
    value = max(float(notional or 0.0), 0.0)
    maker_key = maker_leg(mapping) if execution_mode(mapping) == MAKER_THEN_MARKET else ""
    leg_a_rate = leg_a.maker_fee_rate if maker_key == "a" else leg_a.taker_fee_rate
    leg_b_rate = leg_b.maker_fee_rate if maker_key == "b" else leg_b.taker_fee_rate
    return value * (float(leg_a_rate or 0.0) + float(leg_b_rate or 0.0))


def leg_a_cost_inputs(symbol: str) -> VenueCostInputs:
    """获取 Leg A（Hyperliquid）的综合成本输入。

    从 Hyperliquid API 读取用户费率，结合资产元数据
    计算有效费率（考虑 growth mode 等）。

    参数:
        symbol: 品种名（可含 ``:dex`` 后缀）

    返回:
        VenueCostInputs
    """
    taker, maker = _leg_a_user_fee_rates()
    market_data = _leg_a_market_data(symbol)
    effective_taker, effective_maker, fee_source = _hyperliquid_effective_fee_rates(
        symbol, taker, maker, market_data.asset_meta,
    )
    return VenueCostInputs(
        taker_fee_rate=effective_taker,
        maker_fee_rate=effective_maker,
        source=f"{fee_source}+metaAndAssetCtxs",
    )


# 向后兼容别名
hyperliquid_cost_inputs = leg_a_cost_inputs


def _leg_a_user_fee_rates() -> tuple[float, float]:
    """获取 Hyperliquid 用户实际 Taker/Maker 费率。

    优先从 API 读取 userFees；失败或无地址时使用配置默认值。
    结果通过 TTLCache 缓存。
    """
    settings = get_settings()
    cached = _hl_user_fee_cache.get("user_fees")
    if cached:
        return cached
    account_address = settings.hyperliquid.account_address
    if not account_address:
        return settings.hyperliquid.default_taker_fee_rate, settings.hyperliquid.default_maker_fee_rate
    try:
        data = post_hyperliquid_info(
            settings.hyperliquid.info_url,
            {"type": "userFees", "user": account_address},
        )
        taker = safe_float(data.get("userCrossRate"), settings.hyperliquid.default_taker_fee_rate)
        maker = safe_float(data.get("userAddRate"), settings.hyperliquid.default_maker_fee_rate)
        _hl_user_fee_cache.set("user_fees", (taker, maker))
        return taker, maker
    except Exception as exc:
        logger.warning("Hyperliquid userFees 读取失败，使用默认费率: {}", exc)
        return settings.hyperliquid.default_taker_fee_rate, settings.hyperliquid.default_maker_fee_rate


def _leg_a_market_data(symbol: str = "") -> HyperliquidMarketData:
    """获取用于确定手续费倍率的 Hyperliquid 资产元数据。

    通过 ``metaAndAssetCtxs`` 接口读取所有品种的资金费率和元信息。
    结果按 dex 分组缓存。
    """
    settings = get_settings()
    dex = symbol.split(":", 1)[0] if ":" in symbol else ""
    cache_key = f"market_{dex}"
    cached = _hl_market_cache.get(cache_key)
    if cached:
        return cached
    try:
        payload: dict = {"type": "metaAndAssetCtxs"}
        if dex:
            payload["dex"] = dex
        meta, _contexts = post_hyperliquid_info(settings.hyperliquid.info_url, payload)
        asset_meta: dict[str, dict] = {}
        for asset in meta.get("universe", []):
            name = asset.get("name", "")
            asset_meta[name] = asset
        market_data = HyperliquidMarketData(asset_meta)
        _hl_market_cache.set(cache_key, market_data)
        return market_data
    except Exception as exc:
        logger.warning("Hyperliquid 资产费率元数据读取失败，使用保守费率倍率: {}", exc)
        return HyperliquidMarketData({})


def _hyperliquid_effective_fee_rates(
    symbol: str,
    taker: float,
    maker: float,
    asset_meta: dict[str, dict],
) -> tuple[float, float, str]:
    """根据品种和资产元数据计算有效费率。

    - 普通品种（不含 ``:``）：直接使用用户费率
    - ``xyz`` DEX 品种：growth mode 下费率 ×0.2，标准模式 ×2.0
    - HIP3 品种：growth mode ×0.2，标准模式 ×2.0
    """
    if ":" not in symbol:
        return taker, maker, "hyperliquid_userFees"

    dex = symbol.split(":", 1)[0]
    meta = asset_meta.get(symbol, {})
    growth_mode = str(meta.get("growthMode", "")).lower() == "enabled"

    if dex == "xyz":
        if not meta:
            return taker * 0.2, maker * 0.2, "hyperliquid_userFees+xyz_growth_fee_multiplier_fallback"
        multiplier = 0.2 if growth_mode else 2.0
        mode = "growth" if growth_mode else "standard"
        return taker * multiplier, maker * multiplier, f"hyperliquid_userFees+xyz_{mode}_fee_multiplier"

    if growth_mode:
        return taker * 0.2, maker * 0.2, "hyperliquid_userFees+hip3_growth_conservative_fee_multiplier"
    return taker * 2.0, maker * 2.0, "hyperliquid_userFees+hip3_standard_fee_multiplier"
