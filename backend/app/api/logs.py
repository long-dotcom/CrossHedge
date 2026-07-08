"""
日志与告警路由
==============

- GET  /logs                   —— 系统日志列表（分页，可按级别/关键字过滤）
- GET  /alerts                 —— 告警列表（分页）
- POST /alerts/{alert_id}/ack  —— 确认告警

注意：这些端点都在 /api 根级别，不在子前缀下。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import as_dict, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import Alert, SystemLog, User
from app.db.session import get_db

# 无子前缀，路由直接挂在 /api 下
router = APIRouter()


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("/logs")
def logs(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
    level: str = "",
    keyword: str = "",
) -> dict[str, Any]:
    """系统日志列表（分页，可按级别/关键字过滤）。"""
    return _logs_payload(db, page=page, page_size=page_size, level=level, keyword=keyword)


def _logs_payload(db: Session, page: int = 1, page_size: int = 20, level: str = "", keyword: str = "") -> dict[str, Any]:
    """组装系统日志列表。"""
    query = db.query(SystemLog)
    if level:
        query = query.filter(SystemLog.level == level)
    if keyword:
        query = query.filter(SystemLog.message.contains(keyword))
    total = query.count()
    rows = query.order_by(desc(SystemLog.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


@router.get("/alerts")
def alerts(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """告警列表（分页）。"""
    return _alerts_payload(db, page=page, page_size=page_size)


def _alerts_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """组装告警列表。"""
    query = db.query(Alert)
    total = query.count()
    rows = query.order_by(desc(Alert.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


@router.post("/alerts/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """确认告警。"""
    row = db.get(Alert, alert_id)
    if not row:
        raise HTTPException(status_code=404, detail="告警不存在")
    row.acknowledged = True
    audit(db, user.id, "ack_alert", "alert", str(alert_id))
    db.commit()
    return as_dict(row)
