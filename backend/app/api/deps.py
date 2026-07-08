"""
API 公共依赖与工具函数
======================

从原 router.py 中提取的、被多个路由模块共享的辅助函数：

- ``as_dict``            —— ORM 对象 → dict
- ``_leg_metadata``      —— 品种映射 → 双腿元信息
- ``_paginate_rows``     —— 内存分页
- ``bearer_token_from_request`` —— 从请求头提取 Bearer Token
- ``audit``              —— 写审计日志
- ``json_default``       —— JSON 序列化日期回退
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.db.models import AuditLog, SymbolMapping
from app.execution.hedge_pool import HedgeGroupSnapshot


# ---------------------------------------------------------------------------
# ORM → dict
# ---------------------------------------------------------------------------

def as_dict(row: Any) -> dict[str, Any]:
    """将 ORM 模型实例转换为普通字典。

    对 ``HedgeGroupSnapshot``（dataclass）做特殊处理，
    其余 ORM 对象通过 ``__table__.columns`` 逐列读取。
    """
    if isinstance(row, HedgeGroupSnapshot):
        return dict(row.__dict__)
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    return data


# ---------------------------------------------------------------------------
# 品种映射 → 双腿元信息
# ---------------------------------------------------------------------------

def _leg_metadata(mapping: SymbolMapping | None) -> dict[str, str]:
    """根据 SymbolMapping 返回双腿 venue / symbol 元信息。"""
    if not mapping:
        return {
            "leg_a_venue": "hyperliquid",
            "leg_a_symbol": "",
            "leg_b_venue": "mt5",
            "leg_b_symbol": "",
        }
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
    return {
        "leg_a_venue": leg_a_venue or "hyperliquid",
        "leg_a_symbol": leg_a_symbol or mapping.symbol,
        "leg_b_venue": leg_b_venue or "mt5",
        "leg_b_symbol": leg_b_symbol or mapping.symbol,
    }


def _leg_metadata_for_symbol(db: Session, symbol: str) -> dict[str, str]:
    """按内部品种名查 SymbolMapping，返回双腿元信息。"""
    mapping = db.query(SymbolMapping).filter(
        SymbolMapping.symbol == str(symbol or "").upper()
    ).first()
    return _leg_metadata(mapping)


def _row_with_leg_metadata(db: Session, row: Any) -> dict[str, Any]:
    """在 as_dict 基础上附加双腿元信息列。"""
    data = as_dict(row) if not isinstance(row, dict) else dict(row)
    data.update(_leg_metadata_for_symbol(db, str(data.get("symbol") or "")))
    return data


# ---------------------------------------------------------------------------
# 分页
# ---------------------------------------------------------------------------

def _paginate_rows(rows: list[dict[str, Any]], page: int, page_size: int) -> dict[str, Any]:
    """对已加载到内存的行列表做分页。"""
    safe_page = max(int(page), 1)
    safe_page_size = max(int(page_size), 1)
    start = (safe_page - 1) * safe_page_size
    return {"total": len(rows), "items": rows[start : start + safe_page_size]}


# ---------------------------------------------------------------------------
# 已启用品种名集合
# ---------------------------------------------------------------------------

def _enabled_symbol_names(db: Session) -> set[str]:
    """返回当前已启用的内部品种名（大写）集合。"""
    return {
        row.symbol.upper()
        for row in db.query(SymbolMapping.symbol)
        .filter(SymbolMapping.enabled.is_(True))
        .all()
    }


# ---------------------------------------------------------------------------
# JSON 序列化辅助
# ---------------------------------------------------------------------------

def json_default(value: Any) -> str:
    """json.dumps 的 default 回退，处理 datetime / date。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Bearer Token 提取（SSE 流使用，因为 SSE 不走 Depends）
# ---------------------------------------------------------------------------

def bearer_token_from_request(request: Request) -> str:
    """从 Authorization 头提取 Bearer Token，缺失则抛 401。"""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    return token


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

def audit(
    db: Session,
    user_id: int | None,
    action: str,
    resource: str,
    detail: str = "",
    request: Request | None = None,
) -> None:
    """写入一条审计日志（不 commit，由调用方决定提交时机）。"""
    db.add(AuditLog(
        user_id=user_id,
        action=action,
        resource=resource,
        detail=detail,
        ip_address=request.client.host if request and request.client else "",
    ))
