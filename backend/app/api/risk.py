"""
风控路由
========

- GET  /risk/status          —— 当前风控设置
- GET  /risk/events          —— 风控事件列表（分页）
- POST /risk/mode            —— 切换风控模式
- POST /risk/emergency-stop  —— 紧急停止
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import as_dict, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import Alert, RiskEvent, RiskSetting, User
from app.db.session import get_db
from app.schemas import RiskModeIn

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _risk_status_payload(db: Session) -> dict[str, Any]:
    """返回当前风控设置。"""
    risk = db.query(RiskSetting).first()
    return as_dict(risk) if risk else {}


def _risk_events_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """组装风控事件列表（分页）。"""
    query = db.query(RiskEvent)
    total = query.count()
    rows = query.order_by(desc(RiskEvent.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("/status")
def risk_status(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """当前风控设置。"""
    return _risk_status_payload(db)


@router.get("/events")
def risk_events(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """风控事件列表（分页）。"""
    return _risk_events_payload(db, page=page, page_size=page_size)


@router.post("/mode")
def set_risk_mode(
    payload: RiskModeIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """切换风控模式。"""
    if payload.mode not in {"normal", "reduce_only", "paused", "emergency_stop"}:
        raise HTTPException(status_code=400, detail="无效风控模式")
    risk = db.query(RiskSetting).first() or RiskSetting()
    risk.mode = payload.mode
    db.add(risk)
    audit(db, user.id, "set_risk_mode", "risk", payload.mode)
    db.commit()
    return as_dict(risk)


@router.post("/emergency-stop")
def emergency_stop(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """触发紧急停止，禁止自动下单。"""
    risk = db.query(RiskSetting).first() or RiskSetting()
    risk.mode = "emergency_stop"
    db.add(risk)
    db.add(Alert(level="critical", title="紧急停止", message="管理员触发紧急停止，系统禁止自动下单"))
    audit(db, user.id, "emergency_stop", "risk")
    db.commit()
    return {"status": "emergency_stop"}
