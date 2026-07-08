"""
认证路由
========

- POST /auth/login  —— 管理员登录，返回 JWT
- POST /auth/logout —— 登出，写审计日志
- GET  /auth/me     —— 获取当前用户信息
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import audit
from app.auth.dependencies import get_current_user
from app.auth.security import create_access_token, verify_password
from app.db.models import User
from app.db.session import get_db
from app.schemas import LoginRequest, TokenResponse

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TokenResponse:
    """管理员登录，校验密码后签发 JWT。"""
    user = db.query(User).filter(
        User.username == payload.username,
        User.is_active.is_(True),
    ).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    audit(db, user.id, "login", "auth", "管理员登录", request)
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.username, {"role": user.role}),
        user={"username": user.username, "role": user.role},
    )


@router.post("/logout")
def logout(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """管理员登出。"""
    audit(db, user.id, "logout", "auth")
    db.commit()
    return {"status": "ok"}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """返回当前登录用户信息。"""
    return {"username": user.username, "role": user.role}
