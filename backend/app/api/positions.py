"""
持仓管理路由
============

- GET  /positions                    —— 持仓列表
- POST /positions/{position_id}/adopt —— 接管外部仓位
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.venue import NATIVE_VENUES, mapping_leg
from app.api.deps import _leg_metadata_for_symbol, _row_with_leg_metadata, as_dict, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import HedgeGroup, HedgeGroupEvent, Position, SymbolMapping, User
from app.db.session import get_db
from app.positions.live import live_positions_payload
from app.schemas import AdoptPositionIn

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _positions_payload(db: Session) -> list[dict[str, Any]]:
    """返回所有持仓（含双腿元信息）。"""
    return [
        _row_with_leg_metadata(db, r)
        for r in db.query(Position).order_by(desc(Position.created_at)).all()
    ]


def _mapping_for_position(db: Session, position: Position, requested_symbol: str = "") -> SymbolMapping | None:
    """根据仓位信息查找对应的品种映射。"""
    symbol = requested_symbol.strip().upper()
    if symbol:
        return db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
    for mapping in db.query(SymbolMapping).all():
        leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
        leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
        if position.platform == leg_a_venue and position.symbol in {mapping.symbol, leg_a_symbol}:
            return mapping
        if position.platform == leg_b_venue and position.symbol in {mapping.symbol, leg_b_symbol}:
            return mapping
        if position.symbol == mapping.symbol:
            return mapping
    return None


def _direction_for_position(position: Position, mapping: SymbolMapping) -> str:
    """根据仓位方向和所在 venue 推断对冲方向。"""
    side = position.side.lower()
    leg_a_venue, _ = mapping_leg(mapping, "a")
    is_leg_a = position.platform == leg_a_venue
    if is_leg_a:
        return "long_leg_a_short_leg_b" if side == "long" else "long_leg_b_short_leg_a"
    return "long_leg_b_short_leg_a" if side == "long" else "long_leg_a_short_leg_b"


def _position_has_live_group(db: Session, position: Position, mapping: SymbolMapping) -> bool:
    """检查该仓位是否已有对应的 live 对冲组。"""
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live").all()
    leg_a_venue, leg_a_sym = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_sym = mapping_leg(mapping, "b")
    platform_symbols = {
        leg_a_venue: {mapping.symbol, leg_a_sym},
        leg_b_venue: {mapping.symbol, leg_b_sym},
    }
    if position.symbol not in platform_symbols.get(position.platform, set()):
        return False
    return any(group.symbol == mapping.symbol and group.status != "closed" for group in groups)


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("")
def positions(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """实时持仓列表。

    页面展示直接读取各 adapter 当前状态；非原生交易所从 Nautilus runtime/cache 读取。
    """
    return live_positions_payload(db)


@router.post("/{position_id}/adopt")
def adopt_position(
    position_id: int,
    payload: AdoptPositionIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """接管外部仓位，创建对应的对冲组。"""
    position = db.get(Position, position_id)
    if not position:
        raise HTTPException(status_code=404, detail="仓位不存在")
    if position.platform not in NATIVE_VENUES:
        raise HTTPException(status_code=400, detail=f"只支持接管 {NATIVE_VENUES} live 仓位")
    if abs(position.quantity) <= 0:
        raise HTTPException(status_code=400, detail="仓位数量为 0，不能接管")

    mapping = _mapping_for_position(db, position, payload.symbol)
    if not mapping:
        raise HTTPException(status_code=400, detail="找不到该仓位对应的品种映射，请先配置 symbol mapping 或在请求中指定内部 symbol")
    if _position_has_live_group(db, position, mapping):
        raise HTTPException(status_code=400, detail="该仓位已匹配 live 对冲组，不能重复接管")

    direction = _direction_for_position(position, mapping)
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_a_quantity = position.quantity if position.platform == leg_a_venue else 0.0
    leg_b_quantity = position.quantity if position.platform != leg_a_venue else 0.0
    notional = abs(position.quantity * (position.mark_price or position.entry_price or 0.0))

    group = HedgeGroup(
        symbol=mapping.symbol,
        direction=direction,
        status="manual_intervention",
        execution_mode="live",
        notional=notional,
        quantity=abs(position.quantity),
        leg_a_quantity=leg_a_quantity,
        leg_b_quantity=leg_b_quantity,
        unrealized_pnl=position.unrealized_pnl,
        close_reason=f"外部仓位接管: {payload.reason}",
        source=user.username,
        opened_at=position.created_at,
    )
    db.add(group)
    db.flush()

    detail = f"{position.platform}:{position.symbol}:{position.side}:{position.quantity}"
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="adopted_external_position", detail=detail))
    audit(db, user.id, "adopt_position", "position", f"{position_id}->{group.id}: {detail}")
    db.commit()
    db.refresh(group)

    # 引入对冲组 payload 组装（避免循环导入，延迟导入）
    from app.api.hedge_groups import _hedge_group_payload
    return _hedge_group_payload(db, group)
