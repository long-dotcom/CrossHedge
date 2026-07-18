"""从原生交易所连接器实时读取持仓，不把展示快照写回业务持仓表。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.api.deps import _leg_metadata_for_symbol
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ExchangeCredential, SymbolMapping
from app.venues.domain.models import Position, PositionSide
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)
SUPPORTED_VENUES = {"hyperliquid", "mt5", "binance"}


def live_positions_payload(db: Session) -> list[dict[str, Any]]:
    """实时读取所有已配置交易所的当前持仓。"""
    rows: list[dict[str, Any]] = []
    for venue in _configured_venues(db):
        try:
            connector = native_venue_manager.connector_for(venue, "live")
            for position in connector.get_positions():
                if position.quantity:
                    rows.append(_position_payload_row(db, position))
        except Exception as exc:
            logger.warning("实时持仓读取失败: venue={}, error={}", venue, exc)
    return sorted(rows, key=lambda item: (item["platform"], item["symbol"], item["side"]))


def _configured_venues(db: Session) -> list[str]:
    values = {"hyperliquid", "mt5"}
    for mapping in db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all():
        values.update(
            str(value or "").strip().lower()
            for value in (mapping.leg_a_venue, mapping.leg_b_venue)
        )
    values.update(
        str(venue or "").strip().lower()
        for (venue,) in db.query(ExchangeCredential.venue).filter(ExchangeCredential.enabled.is_(True)).all()
    )
    return sorted(values & SUPPORTED_VENUES)


def _position_payload_row(db: Session, item: Position) -> dict[str, Any]:
    side = _position_side(item)
    mark_price = item.mark_price or item.entry_price
    unrealized_pnl = item.unrealized_pnl or _unrealized_pnl(
        side,
        item.quantity,
        item.entry_price,
        mark_price,
    )
    row = {
        "id": f"live:{item.venue}:{item.symbol}:{side}:{item.external_position_id}",
        "platform": item.venue,
        "symbol": item.symbol,
        "side": side,
        "quantity": float(item.quantity),
        "entry_price": float(item.entry_price),
        "mark_price": float(mark_price),
        "unrealized_pnl": float(unrealized_pnl),
        "margin_used": float(item.margin_used),
        "liquidation_price": float(item.liquidation_price) if item.liquidation_price is not None else None,
        "created_at": _iso(item.observed_at),
        "updated_at": _iso(item.observed_at),
        "hedge_group_id": None,
        "data_source": "native_connector",
        "adoptable": False,
    }
    row.update(_leg_metadata_for_symbol(db, item.symbol))
    return row


def _position_side(item: Position) -> str:
    if item.position_side == PositionSide.LONG:
        return "long"
    if item.position_side == PositionSide.SHORT:
        return "short"
    signed = item.raw.get("positionAmt", item.raw.get("szi", item.quantity))
    try:
        return "short" if Decimal(str(signed)) < 0 else "long"
    except Exception:
        return "long"


def _unrealized_pnl(side: str, quantity: Decimal, entry_price: Decimal, mark_price: Decimal) -> Decimal:
    if quantity <= 0 or entry_price <= 0 or mark_price <= 0:
        return Decimal("0")
    direction = Decimal("-1") if side == "short" else Decimal("1")
    return (mark_price - entry_price) * quantity * direction


def _iso(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or utc_now())
