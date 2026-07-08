"""
实时持仓读取
============

页面和 SSE 展示使用本模块直接读取交易所适配器状态。
非原生交易所的持仓由 NautilusAdapter 从长期 TradingNode cache/report 读取，
这里不把展示数据写入 Position 表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.venue import NATIVE_VENUES, build_market_adapter, nautilus_venues_from_mappings
from app.api.deps import _leg_metadata_for_symbol
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ExchangeCredential, SymbolMapping

logger = get_logger(__name__)


def live_positions_payload(db: Session) -> list[dict[str, Any]]:
    """实时读取所有已配置 venue 的当前持仓。"""
    adapters = _live_position_adapters(db)
    rows: list[dict[str, Any]] = []
    hyperliquid_dexes = _hyperliquid_position_dexes(db)
    for adapter in adapters:
        try:
            positions = adapter.get_positions(dexes=hyperliquid_dexes) if isinstance(adapter, HyperliquidAdapter) else adapter.get_positions()
        except Exception as exc:
            logger.warning("实时持仓读取失败: platform={}, error={}", getattr(adapter, "platform", ""), exc)
            continue
        for item in positions:
            row = _position_payload_row(db, item, getattr(adapter, "platform", ""), adapter)
            if abs(float(row.get("quantity") or 0.0)) <= 0:
                continue
            rows.append(row)
    return sorted(rows, key=lambda item: (str(item.get("platform") or ""), str(item.get("symbol") or ""), str(item.get("side") or "")))


def _live_position_adapters(db: Session) -> list[Any]:
    """构建用于展示的 live adapter 列表。"""
    adapters: list[Any] = [build_market_adapter("hyperliquid", live=True), build_market_adapter("mt5", live=True)]
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = nautilus_venues_from_mappings(mappings)
    for (venue,) in db.query(ExchangeCredential.venue).filter(ExchangeCredential.enabled.is_(True)).all():
        normalized = str(venue or "").strip().lower()
        if normalized not in NATIVE_VENUES and normalized not in venues:
            venues.append(normalized)
    for venue in venues:
        adapters.append(build_market_adapter(venue, live=True))
    return adapters


def _position_payload_row(db: Session, item: dict[str, Any], fallback_platform: str, adapter: Any) -> dict[str, Any]:
    """将 adapter 持仓字典转成前端展示结构。"""
    platform = str(item.get("platform") or fallback_platform)
    symbol = str(item.get("symbol") or "")
    side = str(item.get("side") or "")
    entry_price = float(item.get("entry_price", 0.0) or 0.0)
    mark_price = _live_mark_price(adapter, symbol, float(item.get("mark_price", 0.0) or 0.0), entry_price)
    unrealized_pnl = float(item.get("unrealized_pnl", 0.0) or 0.0)
    if not unrealized_pnl:
        unrealized_pnl = _unrealized_pnl(side, float(item.get("quantity", 0.0) or 0.0), entry_price, mark_price)
    row = {
        "id": f"live:{platform}:{symbol}:{side}",
        "platform": platform,
        "symbol": symbol,
        "side": side,
        "quantity": float(item.get("quantity", 0.0) or 0.0),
        "entry_price": entry_price,
        "mark_price": mark_price,
        "unrealized_pnl": unrealized_pnl,
        "margin_used": float(item.get("margin_used", 0.0) or 0.0),
        "liquidation_price": item.get("liquidation_price"),
        "created_at": _iso(item.get("created_at") or utc_now()),
        "updated_at": _iso(item.get("updated_at") or utc_now()),
        "hedge_group_id": item.get("hedge_group_id"),
        "data_source": "live_adapter",
        "adoptable": False,
    }
    row.update(_leg_metadata_for_symbol(db, symbol))
    return row


def _live_mark_price(adapter: Any, symbol: str, current: float, entry_price: float) -> float:
    """优先从 adapter 实时行情补当前价。"""
    mark_method = getattr(adapter, "get_mark_price", None)
    if callable(mark_method):
        try:
            mark = float(mark_method(symbol) or 0.0)
            if mark > 0:
                return mark
        except Exception as exc:
            logger.debug("实时 mark price 读取失败: platform={}, symbol={}, error={}", getattr(adapter, "platform", ""), symbol, exc)
    ticker_method = getattr(adapter, "get_ticker", None)
    if callable(ticker_method):
        try:
            ticker = ticker_method(symbol)
            bid = float(getattr(ticker, "bid", 0.0) or 0.0)
            ask = float(getattr(ticker, "ask", 0.0) or 0.0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return bid or ask or current or entry_price
        except Exception as exc:
            logger.debug("实时 ticker 读取失败: platform={}, symbol={}, error={}", getattr(adapter, "platform", ""), symbol, exc)
    return current or entry_price


def _unrealized_pnl(side: str, quantity: float, entry_price: float, mark_price: float) -> float:
    """用当前价估算线性合约/现货持仓未实现盈亏。"""
    if quantity <= 0 or entry_price <= 0 or mark_price <= 0:
        return 0.0
    direction = -1.0 if str(side or "").lower() == "short" else 1.0
    return (mark_price - entry_price) * quantity * direction


def _hyperliquid_position_dexes(db: Session) -> list[str]:
    """从品种映射中提取 Hyperliquid DEX 列表。"""
    dexes: list[str] = []
    rows = db.query(SymbolMapping.leg_a_venue_symbol).filter(SymbolMapping.enabled.is_(True)).all()
    for (symbol,) in rows:
        value = str(symbol or "")
        if ":" not in value:
            continue
        dex = value.split(":", 1)[0].strip()
        if dex and dex not in dexes:
            dexes.append(dex)
    return dexes


def _iso(value: Any) -> str:
    """将时间字段转成 ISO 字符串。"""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
