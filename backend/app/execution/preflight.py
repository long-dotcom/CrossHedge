"""执行 Intent 创建前的只读预检与价格规范化工具。"""

from __future__ import annotations

import math
from threading import RLock

from sqlalchemy.orm import Session

from app.db.models import ArbitrageOpportunity, StrategySetting, SymbolMapping, SystemSetting
from app.execution.readiness import live_execution_readiness, paper_execution_readiness
from app.market.active_refresh import refresh_execution_quotes
from app.market.quotes import quote_synchronizer
from app.strategy.spread_math import spreads_for_direction


OPEN_CAPACITY_LOCK = RLock()


def live_trading_enabled(db: Session) -> bool:
    """读取数据库中的实盘总开关。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return bool(row and row.value == "true")


def require_live_execution_ready(db: Session) -> None:
    """阻止任何未通过实盘就绪检查的 Intent。"""
    readiness = live_execution_readiness(db)
    blocked = [item for item in readiness.get("checks", []) if item.get("status") == "block"]
    if blocked:
        detail = "; ".join(str(item.get("message") or item.get("component")) for item in blocked)
        raise ValueError(f"实盘执行就绪检查未通过: {detail}")


def require_paper_execution_ready(db: Session) -> None:
    """阻止任何未通过完整 Paper 环境检查的 Intent。"""
    readiness = paper_execution_readiness(db)
    blocked = [item for item in readiness.get("checks", []) if item.get("status") == "block"]
    if blocked:
        detail = "; ".join(str(item.get("message") or item.get("component")) for item in blocked)
        raise ValueError(f"paper 完整模拟执行就绪检查未通过: {detail}")


def strict_sync_for_execution(mapping: SymbolMapping, symbol: str, settings) -> tuple[object | None, str, bool]:
    """检查严格同步报价；失败时仅主动刷新一次后重新判断。"""
    synced, reason = quote_synchronizer.synchronized(
        symbol,
        mode="strict",
        max_time_diff_ms=settings.quote.strict_sync_ms,
        max_age_ms=settings.quote.stale_ms,
        leg_a_venue=mapping.leg_a_venue,
        leg_b_venue=mapping.leg_b_venue,
    )
    if synced:
        return synced, reason, False
    refreshed = refresh_execution_quotes(mapping)
    if not refreshed:
        return synced, reason, False
    synced, reason = quote_synchronizer.synchronized(
        symbol,
        mode="strict",
        max_time_diff_ms=settings.quote.strict_sync_ms,
        max_age_ms=settings.quote.stale_ms,
        leg_a_venue=mapping.leg_a_venue,
        leg_b_venue=mapping.leg_b_venue,
    )
    if not synced:
        return None, f"{reason}；执行前主动刷新: {','.join(refreshed)}", True
    return synced, "", True


def refreshed_opportunity_still_executable(
    opportunity: ArbitrageOpportunity,
    synced,
    strategy: StrategySetting,
) -> tuple[bool, str]:
    """使用执行前当前 BBO 重新计算入场线和最低净利润。"""
    spread = spreads_for_direction(
        opportunity.direction,
        synced.leg_a.bid,
        synced.leg_a.ask,
        synced.leg_b.bid,
        synced.leg_b.ask,
    ).entry_spread
    threshold = float(opportunity.entry_threshold or 0.0)
    if threshold > 0 and spread < threshold:
        return False, f"执行前当前价差不再满足入场线: {spread:.6f} < {threshold:.6f}"
    quantity = float(opportunity.leg_a_quantity or opportunity.quantity or 0.0)
    net_profit = (spread - float(opportunity.unit_cost or 0.0)) * quantity
    minimum = max(float(strategy.min_total_profit or 0.0), float(strategy.min_net_profit or 0.0))
    if net_profit < minimum:
        return False, f"执行前当前净利润不足: {net_profit:.2f} < {minimum:.2f}"
    return True, ""


def maker_price(side: str, bid: float, ask: float, offset_bps: float, mapping: SymbolMapping | None = None) -> float:
    """计算并按 venue tick/精度规范化 Post-only 限价。"""
    raw = bid * (1 - offset_bps / 10_000) if side == "buy" else ask * (1 + offset_bps / 10_000)
    if raw <= 0:
        return raw
    tick = float(getattr(mapping, "min_tick", 0.0) or 0.0) if mapping else 0.0
    if tick > 0:
        units = raw / tick
        raw = math.floor(units) * tick if side == "buy" else math.ceil(units) * tick
    precision = max(min(int(getattr(mapping, "price_precision", 9) if mapping else 9), 9), 0)
    return round(raw, precision)
