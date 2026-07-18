"""
实时成本数据模块
================

按 venue 自动获取实时费率数据，包括：

- Hyperliquid 用户费率（userFees）
- Hyperliquid 资金费率（metaAndAssetCtxs）
- 原生连接器返回的 maker/taker 费率和永续 funding
- MT5 品种隔夜利息

使用 ``TTLCache`` 缓存结果，避免频繁请求。

使用方式::

    from app.strategy.live_costs import leg_a_cost_inputs, mt5_cost_inputs

    hl_costs = leg_a_cost_inputs("ETH")
    mt5_costs = mt5_cost_inputs("BTCUSD", "buy", 0.01, 0.5)
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.config.settings import get_settings
from app.core.cache import TTLCache
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.core.mt5_bootstrap import ensure_mt5_connected
from app.core.type_utils import safe_float
from app.market.fx import fx_to_usd

logger = get_logger(__name__)


@dataclass
class VenueCostInputs:
    """交易所（Venue）成本输入参数。

    属性:
        taker_fee_rate: Taker 费率
        maker_fee_rate: Maker 费率
        funding_rate: 资金费率
        funding_interval_hours: 资金费率对应的结算周期（小时）
        source: 数据来源标识
    """
    taker_fee_rate: float
    maker_fee_rate: float
    funding_rate: float
    source: str
    funding_interval_hours: float = 1.0


# 向后兼容别名
HyperliquidCostInputs = VenueCostInputs


@dataclass
class MT5CostInputs:
    """MT5 成本输入参数。

    属性:
        commission_rate: 佣金率
        swap_cost: 隔夜利息成本（已折算为 USD）
        swap_long: 多头隔夜利息原始值
        swap_short: 空头隔夜利息原始值
        swap_mode: 利息模式（0=金额, 1=点数, ...）
        source: 数据来源标识
    """
    commission_rate: float
    swap_cost: float
    swap_long: float
    swap_short: float
    swap_mode: int
    source: str


@dataclass
class HyperliquidMarketData:
    """Hyperliquid 市场数据（资金费率 + 资产元数据）。"""
    funding_rates: dict[str, float]
    asset_meta: dict[str, dict]


# ── TTL 缓存实例 ────────────────────────────────────────────────────────────
# 市场数据缓存（按 dex 分组），TTL 由 settings.cost.cost_cache_ttl_seconds 控制
_cost_cache_ttl = max(float(get_settings().cost.cost_cache_ttl_seconds), 1.0)
_hl_market_cache: TTLCache[HyperliquidMarketData] = TTLCache(ttl_seconds=_cost_cache_ttl)
# 用户费率缓存，TTL 同上
_hl_user_fee_cache: TTLCache[tuple[float, float]] = TTLCache(ttl_seconds=_cost_cache_ttl)
# 交易所成本缓存，避免扫描循环反复请求品种与 funding。
_venue_cost_cache: TTLCache[VenueCostInputs] = TTLCache(ttl_seconds=_cost_cache_ttl)


class VenueCostUnavailable(RuntimeError):
    """venue 费率或 funding 无法自动获取。"""


def venue_cost_inputs(venue: str, symbol: str) -> VenueCostInputs:
    """按 venue 自动获取 maker/taker 费率和 funding。

    不支持或查询失败时抛出异常，调用方必须阻止成本未知的候选进入执行，
    禁止静默套用 Hyperliquid 费率或按零成本处理。
    """
    normalized = str(venue or "").strip().lower()
    if normalized == "hyperliquid":
        return leg_a_cost_inputs(symbol)
    if normalized == "mt5":
        return VenueCostInputs(0.0, 0.0, 0.0, "mt5_no_trading_fee", 1.0)

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
            funding_rate=safe_float(instrument.funding_rate),
            funding_interval_hours=8.0 if normalized == "binance" else 1.0,
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

    从 Hyperliquid API 读取用户费率和资金费率，结合资产元数据
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
        funding_rate=market_data.funding_rates.get(symbol, 0.00010),
        funding_interval_hours=1.0,
        source=f"{fee_source}+metaAndAssetCtxs",
    )


# 向后兼容别名
hyperliquid_cost_inputs = leg_a_cost_inputs


def mt5_cost_inputs(mt5_symbol: str, mt5_side: str, quantity: float, holding_days: float) -> MT5CostInputs:
    """自动获取 MT5 品种隔夜利息；账户佣金固定按 0 处理。

    参数:
        mt5_symbol: MT5 品种名
        mt5_side: 交易方向（``"buy"`` / ``"sell"``）
        quantity: 交易数量（手数）
        holding_days: 预估持仓天数

    返回:
        MT5CostInputs
    """
    settings = get_settings()
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        logger.warning("MetaTrader5 包不可用，使用默认 MT5 成本: {}", exc)
        return MT5CostInputs(0.0, 0.0, 0.0, 0.0, 0, "mt5_swap_unavailable")

    if not ensure_mt5_connected(
        login=settings.mt5.login,
        password=settings.mt5.password,
        server=settings.mt5.server,
    ):
        logger.warning("MT5 连接失败，使用默认 MT5 成本")
        return MT5CostInputs(0.0, 0.0, 0.0, 0.0, 0, "mt5_swap_unavailable")
    mt5.symbol_select(mt5_symbol, True)
    info = mt5.symbol_info(mt5_symbol)
    if not info:
        return MT5CostInputs(0.0, 0.0, 0.0, 0.0, 0, "mt5_swap_unavailable")
    swap_long = safe_float(getattr(info, "swap_long", 0.0))
    swap_short = safe_float(getattr(info, "swap_short", 0.0))
    swap_mode = int(getattr(info, "swap_mode", 0))
    point = safe_float(getattr(info, "point", 0.0))
    contract_size = safe_float(getattr(info, "trade_contract_size", 1.0))
    tick = mt5.symbol_info_tick(mt5_symbol)
    current_price = (
        (safe_float(getattr(tick, "bid", 0.0)) + safe_float(getattr(tick, "ask", 0.0))) / 2
        if tick
        else 0.0
    )
    account = mt5.account_info()
    currency_by_mode = {
        2: str(getattr(info, "currency_base", "") or "USD"),
        3: str(getattr(info, "currency_margin", "") or "USD"),
        4: str(getattr(account, "currency", "") or "USD"),
    }
    swap_currency = currency_by_mode.get(swap_mode, "USD")
    currency_rate_to_usd = fx_to_usd(swap_currency).rate_to_usd if swap_mode in currency_by_mode else 1.0
    selected_swap = swap_long if mt5_side == "buy" else swap_short
    swap_cost = _estimate_mt5_swap_cost(
        selected_swap,
        swap_mode,
        point,
        contract_size,
        quantity,
        holding_days,
        current_price=current_price,
        currency_rate_to_usd=currency_rate_to_usd,
    )
    return MT5CostInputs(
        commission_rate=0.0,
        swap_cost=swap_cost,
        swap_long=swap_long,
        swap_short=swap_short,
        swap_mode=swap_mode,
        source=f"mt5_symbol_info_swap_mode_{swap_mode}",
    )


def _estimate_mt5_swap_cost(
    swap_value: float,
    swap_mode: int,
    point: float,
    contract_size: float,
    quantity: float,
    holding_days: float,
    *,
    current_price: float = 0.0,
    currency_rate_to_usd: float = 1.0,
) -> float:
    """估算 MT5 隔夜利息成本。

    支持 MT5 的 points、货币、年化利率和 reopen 模式；负值表示支付，
    正值表示收取。holding_days 表示期望持仓天数，因此结果是期望成本。
    """
    if swap_mode == 0 or holding_days <= 0:
        return 0.0
    if swap_mode in {1, 7, 8}:
        # points / reopen：swap 参数以价格点数表示。
        swap_pnl = swap_value * point * contract_size * quantity * holding_days
        return -swap_pnl
    if swap_mode in {2, 3, 4}:
        # 指定货币金额/手/天，统一折算为 USD。
        swap_pnl = swap_value * quantity * holding_days * currency_rate_to_usd
        return -swap_pnl
    if swap_mode in {5, 6}:
        # 年化百分比，MT5 使用 360 天银行年；open 模式在估算阶段用当前价近似。
        notional = current_price * contract_size * quantity
        swap_pnl = notional * (swap_value / 100.0) * (holding_days / 360.0)
        return -swap_pnl
    raise ValueError(f"不支持的 MT5 swap_mode: {swap_mode}")


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
    """获取 Hyperliquid 资金费率和资产元数据。

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
        meta, contexts = post_hyperliquid_info(settings.hyperliquid.info_url, payload)
        rates: dict[str, float] = {}
        asset_meta: dict[str, dict] = {}
        for asset, context in zip(meta.get("universe", []), contexts):
            name = asset.get("name", "")
            rates[name] = safe_float(context.get("funding", 0.0))
            asset_meta[name] = asset
        market_data = HyperliquidMarketData(rates, asset_meta)
        _hl_market_cache.set(cache_key, market_data)
        return market_data
    except Exception as exc:
        logger.warning("Hyperliquid funding 读取失败，使用默认 funding: {}", exc)
        return HyperliquidMarketData({}, {})


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
