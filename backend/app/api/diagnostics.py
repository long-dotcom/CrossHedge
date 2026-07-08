"""
诊断路由
========

- GET /diagnostics/pipeline —— Pipeline 诊断信息
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.db.models import User
from app.db.session import get_db
from app.diagnostics.pipeline import build_pipeline_diagnostics

router = APIRouter()


@router.get("/pipeline")
def pipeline_diagnostics(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Pipeline 诊断信息。"""
    return build_pipeline_diagnostics(db)
