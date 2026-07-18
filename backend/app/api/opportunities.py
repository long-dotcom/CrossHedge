"""
套利机会路由
============

- GET  /opportunities                    —— 候选机会列表（分页）
- GET  /opportunities/{opportunity_id}   —— 机会详情
- POST /opportunities/{opportunity_id}/execute —— 执行机会
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import (
    _enabled_symbol_names,
    _paginate_rows,
    _row_with_leg_metadata,
    as_dict,
)
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import ArbitrageOpportunity, User
from app.db.session import get_db
from app.execution.coordinator import create_open_intent
from app.market.scan_state import scan_state_store

router = APIRouter()


@router.get("")
def opportunities(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """候选套利机会列表（分页）。"""
    enabled_symbols = _enabled_symbol_names(db)
    state = scan_state_store.snapshot()
    if state["ready"]:
        rows = [
            r for r in state["opportunities"]
            if str(r.get("symbol", "")).upper() in enabled_symbols
        ]
        rows = sorted(rows, key=lambda r: r.get("created_at") or datetime.min, reverse=True)
        return _paginate_rows([_row_with_leg_metadata(db, r) for r in rows], page, page_size)

    query = (
        db.query(ArbitrageOpportunity)
        .filter(
            ArbitrageOpportunity.symbol.in_(enabled_symbols),
            ArbitrageOpportunity.status.in_(["candidate", "executable", "executing"]),
        )
        if enabled_symbols
        else db.query(ArbitrageOpportunity).filter(False)
    )
    total = query.count()
    rows = query.order_by(desc(ArbitrageOpportunity.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_row_with_leg_metadata(db, r) for r in rows]}


@router.get("/{opportunity_id}")
def opportunity_detail(
    opportunity_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """套利机会详情。"""
    row = db.get(ArbitrageOpportunity, opportunity_id)
    if not row:
        raise HTTPException(status_code=404, detail="机会不存在")
    return _row_with_leg_metadata(db, row)


@router.post("/{opportunity_id}/execute", status_code=202)
def execute_opportunity(
    opportunity_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    """创建异步 OPEN Intent。"""
    try:
        result = create_open_intent(
            db,
            opportunity_id=opportunity_id,
            requested_by=f"user:{user.id}",
            idempotency_key=idempotency_key,
            source=user.username,
        )
        db.commit()
        return {"accepted": True, "created": result.created, "intent": as_dict(result.intent)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
