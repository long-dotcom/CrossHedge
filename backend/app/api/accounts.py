"""
账户路由
========

- GET /accounts           —— 同步并返回最新账户快照
- GET /accounts/snapshots —— 账户快照历史（分页）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.accounts.sync import sync_account_snapshots, latest_account_snapshots
from app.api.deps import as_dict
from app.auth.dependencies import get_current_user
from app.db.models import AccountSnapshot, User
from app.db.session import get_db

router = APIRouter()


@router.get("")
def accounts(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """同步并返回最新账户快照。"""
    rows = sync_account_snapshots(db)
    return [as_dict(r) for r in rows]


@router.get("/snapshots")
def account_snapshots(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """账户快照历史（分页）。"""
    query = db.query(AccountSnapshot)
    total = query.count()
    rows = query.order_by(desc(AccountSnapshot.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [as_dict(r) for r in rows]}
