"""
Paper-live 探针模块
===================

为 NautilusTrader 等外部 venue 提供 paper-live 探针下单能力。
探针下单使用真实交易所凭证，但以极小名义价值验证下单通路。

使用方式::

    from app.execution.probe import place_nautilus_probe_order, nautilus_probe_supported
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.nautilus_runtime import (
    NautilusTradeMode,
    nautilus_probe_supported,
    nautilus_runtime_manager,
)
from app.db.models import ExchangeCredential, SymbolMapping


# Nautilus 探针当前支持的 venue 集合
NAUTILUS_PROBE_SUPPORTED_VENUES = {"binance"}


def place_nautilus_probe_order(
    db: Session,
    credential: ExchangeCredential,
    order: AdapterOrder,
) -> AdapterOrderResult:
    """根据 venue 分发探针下单请求。

    参数:
        db: 数据库会话。
        credential: 交易所凭证（含加密凭证信息）。
        order: 待提交的适配器订单。

    返回:
        适配器订单结果。
    """
    venue = _venue(credential.venue or order.platform)
    if nautilus_probe_supported(venue):
        runtime = nautilus_runtime_manager.runtime_for(credential)
        return runtime.place_order(
            order,
            NautilusTradeMode.PAPER_PROBE,
            configured_min_base_size=_configured_min_base_size(db, venue, order),
        )
    return AdapterOrderResult(
        False, "", "rejected", 0.0, 0.0, 0.0,
        f"Nautilus venue {venue} 尚未实现 paper_probe 真实探针下单",
    )


def _configured_min_base_size(db: Session, venue: str, order: AdapterOrder) -> float:
    """从品种映射中查找当前 order 对应的最小下单量。"""
    symbols = {
        str(order.symbol or "").strip().upper(),
        str(order.venue_symbol or "").strip().upper(),
    }
    rows = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    for row in rows:
        # 检查 leg_a 是否匹配当前 venue
        if _venue(row.leg_a_venue) == venue:
            candidates = {row.symbol, row.leg_a_symbol, row.leg_a_venue_symbol}
            if symbols & {str(item or "").strip().upper() for item in candidates}:
                return float(row.leg_a_min_base_size or row.min_order_size or 0.0)
        # 检查 leg_b 是否匹配当前 venue
        if _venue(row.leg_b_venue) == venue:
            candidates = {row.symbol, row.leg_b_symbol}
            if symbols & {str(item or "").strip().upper() for item in candidates}:
                return float(row.min_order_size or 0.0)
    return 0.0


def _venue(value: str) -> str:
    """标准化 venue 名称：小写、去空白。"""
    return str(value or "").strip().lower()
