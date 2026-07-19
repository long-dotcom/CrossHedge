"""
对冲组管理路由
==============

- GET  /hedge-groups                    —— 对冲组列表（分页）
- GET  /hedge-groups/{group_id}         —— 对冲组详情（含事件和订单）
- POST /hedge-groups/{group_id}/close   —— 关闭对冲组
- POST /hedge-groups/{group_id}/mark-manual —— 标记为需人工介入
- POST /hedge-groups/{group_id}/void       —— 无真实敞口时作废归档
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import as_dict, _leg_metadata_for_symbol, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import ExecutionEvent, ExecutionIntent, ExecutionLeg, HedgeGroup, Order, User, VenueOrder
from app.db.session import get_db
from app.execution.actions import hedge_group_actions
from app.execution.coordinator import create_close_intent, create_recovery_intent
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.voiding import void_hedge_group
from app.execution.pnl import pnl_from_close_spread
from app.market.hedge_spreads import hedge_group_spreads
from app.schemas import CloseHedgeGroupIn, RecoverHedgeGroupIn, VoidHedgeGroupIn

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _hedge_group_payload(db: Session, group: HedgeGroup | HedgeGroupSnapshot, leg_metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """组装单个对冲组的完整数据（含价差和未实现盈亏）。"""
    data = as_dict(group)
    data.update(leg_metadata or _leg_metadata_for_symbol(db, str(data.get("symbol") or "")))
    spreads = hedge_group_spreads(group)
    data.update(spreads)
    current_close_spread = spreads.get("current_close_spread")
    if group.status in {"open", "open_partial"} and current_close_spread is not None:
        try:
            data["unrealized_pnl"] = pnl_from_close_spread(group, float(current_close_spread))
        except (TypeError, ValueError):
            pass
    data["available_actions"] = hedge_group_actions(db, group)
    data["execution_summary"] = _execution_summary(db, int(data.get("id") or 0))
    return data


def _execution_summary(db: Session, group_id: int) -> dict[str, Any] | None:
    """返回最近一次执行阶段和明确错误，供列表直接展示。"""
    intent = (
        db.query(ExecutionIntent)
        .filter(ExecutionIntent.hedge_group_id == group_id)
        .order_by(ExecutionIntent.id.desc())
        .first()
    )
    if intent is None:
        return None
    latest_event = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.intent_id == intent.id)
        .order_by(ExecutionEvent.id.desc())
        .first()
    )
    orders = (
        db.query(VenueOrder)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .filter(ExecutionLeg.intent_id == intent.id)
        .order_by(VenueOrder.id)
        .all()
    )
    pending = sum(1 for order in orders if order.status in {
        "INITIALIZED", "NEW", "SUBMITTED", "ACCEPTED", "PENDING", "OPEN", "PARTIALLY_FILLED", "UNKNOWN",
    })
    return {
        "intent_id": intent.id,
        "intent_type": intent.intent_type,
        "execution_mode": intent.execution_mode,
        "status": intent.status,
        "error_message": intent.error_message,
        "latest_event_type": latest_event.event_type if latest_event else "",
        "pending_orders": pending,
        "total_orders": len(orders),
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }


def _execution_history(db: Session, group_id: int) -> list[dict[str, Any]]:
    """返回 Intent → Leg → VenueOrder/Event 的完整执行诊断树。"""
    intents = (
        db.query(ExecutionIntent)
        .filter(ExecutionIntent.hedge_group_id == group_id)
        .order_by(ExecutionIntent.id.desc())
        .all()
    )
    result: list[dict[str, Any]] = []
    for intent in intents:
        item = as_dict(intent)
        legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
        leg_items = []
        for leg in legs:
            leg_item = as_dict(leg)
            leg_item["venue_orders"] = [
                as_dict(row) for row in
                db.query(VenueOrder).filter(VenueOrder.execution_leg_id == leg.id).order_by(VenueOrder.id).all()
            ]
            leg_item["events"] = [
                as_dict(row) for row in
                db.query(ExecutionEvent).filter(ExecutionEvent.execution_leg_id == leg.id).order_by(ExecutionEvent.id).all()
            ]
            leg_items.append(leg_item)
        item["legs"] = leg_items
        result.append(item)
    return result


def _hedge_groups_payload(db: Session, page: int = 1, page_size: int = 20, include_voided: bool = False) -> dict[str, Any]:
    """组装对冲组列表（分页）。"""
    query = db.query(HedgeGroup)
    if not include_voided:
        query = query.filter(HedgeGroup.status != "voided")
    total = query.count()
    rows = query.order_by(desc(HedgeGroup.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    from app.api.deps import _leg_metadata_by_symbol
    metadata = _leg_metadata_by_symbol(db, {row.symbol for row in rows})
    items = []
    for row in rows:
        # 执行状态以数据库事件投影为权威，不能被滞后的内存快照覆盖。
        items.append(_hedge_group_payload(db, row, metadata.get(row.symbol.upper())))
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
    include_voided: bool = False,
) -> dict[str, Any]:
    """对冲组列表（分页）。"""
    return _hedge_groups_payload(db, page=page, page_size=page_size, include_voided=include_voided)


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
    data["execution_history"] = _execution_history(db, group_id)
    return data


@router.post("/{group_id}/close", status_code=202)
def close_group(
    group_id: int,
    payload: CloseHedgeGroupIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建异步平仓 Intent；HTTP 请求线程不直接访问交易所。"""
    try:
        result = create_close_intent(
            db,
            group_id=group_id,
            reason=payload.reason,
            requested_by=f"user:{user.id}",
            idempotency_key=idempotency_key,
        )
        audit(db, user.id, "close_hedge_group", "hedge_group", f"{group_id}; force={payload.force}")
        db.commit()
        group = db.get(HedgeGroup, group_id)
        if group is not None:
            hedge_pool.upsert_group(group)
        return {
            "accepted": True,
            "created": result.created,
            "intent": as_dict(result.intent),
            "message": "平仓请求已进入执行队列，请等待成交确认",
        }
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


@router.post("/{group_id}/recover", status_code=202)
def recover_group(
    group_id: int,
    payload: RecoverHedgeGroupIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建仅针对本组确认成交残量的恢复 Intent。"""
    expected = f"RECOVER {group_id}"
    if payload.confirmation != expected:
        raise HTTPException(status_code=400, detail=f"恢复操作必须传 confirmation='{expected}'")
    try:
        result = create_recovery_intent(
            db,
            group_id=group_id,
            reason=payload.reason,
            requested_by=f"user:{user.id}",
            idempotency_key=idempotency_key,
        )
        audit(db, user.id, "recover_hedge_group", "hedge_group", f"{group_id}; intent={result.intent.id}")
        db.commit()
        group = db.get(HedgeGroup, group_id)
        if group is not None:
            hedge_pool.upsert_group(group)
        return {
            "accepted": True,
            "created": result.created,
            "intent": as_dict(result.intent),
            "message": "恢复请求已进入执行队列，仅处理本组已确认成交残量",
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{group_id}/void")
def void_group(
    group_id: int,
    payload: VoidHedgeGroupIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """仅在无真实敞口和结果未知外部订单时软作废异常组。"""
    expected = f"VOID {group_id}"
    if payload.confirmation != expected:
        raise HTTPException(status_code=400, detail=f"作废归档必须传 confirmation='{expected}'")
    try:
        group = void_hedge_group(
            db,
            group_id,
            reason=payload.reason,
            requested_by=f"user:{user.id}",
        )
        audit(db, user.id, "void_hedge_group", "hedge_group", f"{group_id}; reason={payload.reason}")
        db.commit()
        db.refresh(group)
        hedge_pool.upsert_group(group)
        return {
            "status": "ok",
            "message": "对冲组已作废归档，执行审计记录仍保留",
            "group": as_dict(group),
        }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
