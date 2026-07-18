"""MT5 订单预检查的 Redis Gateway 兼容入口。

旧版 MT5Adapter 已移除；真实执行统一使用 ``app.venues.mt5`` 的 Redis 代理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.venues.manager import native_venue_manager


@dataclass(frozen=True)
class MT5DemoCheck:
    allowed: bool
    message: str
    login: str = ""
    server: str = ""


@dataclass(frozen=True)
class MT5OrderCheck:
    allowed: bool
    message: str
    retcode: int | None = None
    request: dict[str, Any] | None = None


def mt5_market_order_check(
    symbol: str,
    side: str,
    quantity: float,
    *,
    reduce_only: bool = False,
    demo: bool = False,
) -> MT5OrderCheck:
    """通过 Redis Stream 请求 Gateway 执行 MT5 ``order_check``。"""
    try:
        connector = native_venue_manager.connector_for("mt5", "live")
        result = connector.raw_call("order_check", {
            "symbol": symbol, "side": side, "quantity": quantity,
            "reduce_only": reduce_only, "demo": demo,
        })
        return MT5OrderCheck(
            bool(result.get("allowed")), str(result.get("message", "")),
            int(result["retcode"]) if result.get("retcode") is not None else None,
            result.get("request"),
        )
    except Exception as exc:
        return MT5OrderCheck(False, f"MT5 Gateway order_check 失败: {exc}")
