"""
认证依赖注入
=============

提供 FastAPI 路由的认证依赖注入函数：

- :func:`get_current_user` —— 从 Bearer Token 中解析当前登录用户
- :func:`require_admin` —— 要求当前用户具有管理员角色

使用方式::

    from fastapi import Depends

    @router.get("/protected")
    def protected_route(user: User = Depends(get_current_user)):
        ...
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.security import decode_access_token
from app.core.logging import get_logger
from app.db.models import User
from app.db.session import get_db

logger = get_logger(__name__)

# Bearer Token 提取器（不自动抛出 401，由 get_current_user 统一处理）
bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    """从 Bearer Token 中解析当前登录用户。

    验证流程：
    1. 检查是否提供了 Authorization 头
    2. 解码并验证 JWT 签名和有效期
    3. 从数据库查找对应用户（需存在且未禁用）

    异常:
        HTTPException 401: 未登录、Token 无效或用户不存在/已禁用。
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录"
        )
    try:
        payload = decode_access_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效"
        ) from exc

    user = (
        db.query(User)
        .filter(User.username == payload.get("sub"), User.is_active.is_(True))
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用"
        )
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求当前用户具有管理员角色。

    异常:
        HTTPException 403: 当前用户不是管理员。
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return user
