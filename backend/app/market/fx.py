"""
外汇汇率模块
============

提供 ``fx_to_usd`` 函数，将任意货币转换为 USD 汇率。

数据来源优先级:
1. MT5 实时 tick（直接/间接报价）
2. 配置中的回退汇率（``settings.cost.fx_fallback_rates``）

使用 ``ensure_mt5_connected`` 替代手写 MT5 初始化，
使用 ``safe_float`` 进行安全的类型转换。

使用方式::

    from app.market.fx import fx_to_usd

    rate = fx_to_usd("JPY")
    print(rate.rate_to_usd, rate.source)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.mt5_bootstrap import ensure_mt5_connected
from app.core.type_utils import safe_float

logger = get_logger(__name__)


@dataclass
class FxRate:
    """汇率结果。

    属性:
        currency: 货币代码（如 ``"JPY"``）
        rate_to_usd: 该货币 → USD 的汇率
        source: 数据来源（``"identity"`` / ``"mt5_tick"`` / ``"fallback"``）
    """
    currency: str
    rate_to_usd: float
    source: str


# 汇率缓存：currency → (timestamp, FxRate)
_cache: dict[str, tuple[float, FxRate]] = {}


def fx_to_usd(currency: str, ttl_seconds: int = 5) -> FxRate:
    """获取指定货币对 USD 的汇率。

    参数:
        currency: 货币代码（如 ``"JPY"``、``"EUR"``）
        ttl_seconds: 缓存有效期（秒），默认 5

    返回:
        FxRate 包含汇率和来源

    异常:
        ValueError: 无法获取汇率时抛出
    """
    normalized = (currency or "USD").upper()
    # USD / USDC 无需转换
    if normalized in {"USD", "USDC"}:
        return FxRate(normalized, 1.0, "identity")
    # 检查缓存
    now = time.time()
    cached = _cache.get(normalized)
    if cached and now - cached[0] < ttl_seconds:
        return cached[1]
    # 尝试从 MT5 获取实时汇率
    rate = _mt5_fx_to_usd(normalized)
    if rate:
        result = FxRate(normalized, rate, "mt5_tick")
        _cache[normalized] = (now, result)
        return result
    # 回退到配置中的静态汇率
    fallback = _fallback_rates().get(normalized)
    if fallback:
        result = FxRate(normalized, float(fallback), "fallback")
        _cache[normalized] = (now, result)
        return result
    raise ValueError(f"缺少 {normalized}->USD 汇率")


def _mt5_fx_to_usd(currency: str) -> float | None:
    """通过 MT5 tick 获取汇率。

    尝试直接报价（{currency}USD）和间接报价（USD{currency}）。
    """
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception:
        return None
    if not ensure_mt5_connected(
        login=get_settings().mt5.login,
        password=get_settings().mt5.password,
        server=get_settings().mt5.server,
    ):
        return None
    direct = f"{currency}USD"
    inverse = f"USD{currency}"
    for symbol, invert in ((direct, False), (inverse, True)):
        if not mt5.symbol_select(symbol, True):
            continue
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            continue
        bid = safe_float(getattr(tick, "bid", 0.0))
        ask = safe_float(getattr(tick, "ask", 0.0))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else max(bid, ask)
        if mid <= 0:
            continue
        return 1 / mid if invert else mid
    return None


def _fallback_rates() -> dict[str, float]:
    """从配置中读取回退汇率表。"""
    try:
        data = json.loads(get_settings().cost.fx_fallback_rates or "{}")
    except Exception:
        return {}
    return {str(key).upper(): float(value) for key, value in data.items()}
