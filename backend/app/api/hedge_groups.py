"""
对冲组管理路由
==============

- GET  /hedge-groups                    —— 对冲组列表（分页）
- GET  /hedge-groups/{group_id}         —— 对冲组详情（含事件和订单）
- POST /hedge-groups/{group_id}/close   —— 关闭对冲组
- POST /hedge-groups/{group_id}/mark-manual —— 标记为需人工介入
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import as_dict, _leg_metadata_for_symbol, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import HedgeGroup, Order, User
from app.db.session import get_db
from app.execution.auto_closer import close_hedge_group_from_pool
from app.execution.engine import close_hedge_group
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.persistence import persist_hedge_pool_events
from app.execution.pnl import pnl_from_close_spread
from app.market.hedge_spreads import hedge_group_spreads
from app.schemas import CloseHedgeGroupIn

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _hedge_group_payload(db: Session, group: HedgeGroup | HedgeGroupSnapshot) -> dict[str, Any]:
    """组装单个对冲组的完整数据（含价差和未实现盈亏）。"""
    data = as_dict(group)
    data.update(_leg_metadata_for_symbol(db, str(data.get("symbol") or "")))
    spreads = hedge_group_spreads(group)
    data.update(spreads)
    current_close_spread = spreads.get("current_close_spread")
    if group.status in {"open", "open_partial"} and current_close_spread is not None:
        try:
            data["unrealized_pnl"] = pnl_from_close_spread(group, float(current_close_spread))
        except (TypeError, ValueError):
            pass
    return data


def _hedge_groups_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """组装对冲组列表（分页）。"""
    query = db.query(HedgeGroup)
    total = query.count()
    rows = query.order_by(desc(HedgeGroup.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    active_by_id = {s.id: s for s in hedge_pool.snapshot_groups()}
    items = []
    for row in rows:
        snapshot = active_by_id.get(row.id)
        items.append(_hedge_group_payload(db, snapshot if snapshot and snapshot.symbol == row.symbol else row))
    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("")
def hedge_groups(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """对冲组列表（分页）。"""
    return _hedge_groups_payload(db, page=page, page_size=page_size)


@router.get("/{group_id}")
def hedge_group_detail(
    group_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """对冲组详情，包含事件历史和关联订单。"""
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="对冲组不存在")
    data = _hedge_group_payload(db, group)
    data["events"] = [as_dict(r) for r in group.events]
    data["orders"] = [as_dict(r) for r in db.query(Order).filter(Order.hedge_group_id == group_id).all()]
    return data


@router.post("/{group_id}/close")
def close_group(
    group_id: int,
    payload: CloseHedgeGroupIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """关闭对冲组。"""
    try:
        current = db.get(HedgeGroup, group_id)
        if not current:
            raise ValueError("对冲组不存在")
        if current.status == "manual_intervention" or current.execution_mode == "live":
            group = close_hedge_group(db, group_id, payload.reason)
        else:
            group = close_hedge_group_from_pool(db, group_id, payload.reason, force_strategy_checks=payload.force)
        audit(db, user.id, "close_hedge_group", "hedge_group", f"{group_id}; force={payload.force}")
        db.commit()
        persist_hedge_pool_events(db)
        return as_dict(group)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{group_id}/mark-manual")
def mark_manual(
    group_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """将对冲组标记为需要人工介入。"""
    group = db.get(HedgeGroup, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="对冲组不存在")
    group.status = "manual_intervention"
    hedge_pool.upsert_group(group)
    audit(db, user.id, "mark_manual", "hedge_group", str(group_id))
    db.commit()
    return as_dict(group)
