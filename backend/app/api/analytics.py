"""
分析路由
========

- GET /analytics/spread-summary  —— 价差汇总统计
- GET /analytics/spread-series   —— 价差时间序列
- GET /analytics/venue-spreads   —— 分 venue 价差分析
- GET /analytics/funding-series  —— 资金费率历史
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.analytics.funding import funding_history
from app.analytics.spreads import downsample_spreads, load_spread_points, summarize_spreads
from app.api.deps import _leg_metadata_for_symbol
from app.auth.dependencies import get_current_user
from app.db.models import User
from app.db.session import get_db

router = APIRouter()


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("/spread-summary")
def spread_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    direction: str = "long_leg_b_short_leg_a",
    range: str = "1h",
    basis: str = "entry",
) -> dict[str, Any]:
    """价差汇总统计。"""
    safe_basis = basis if basis in {"entry", "close", "mid"} else "entry"
    points = load_spread_points(db, symbol, direction, range, basis=safe_basis)
    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "basis": safe_basis,
        **_leg_metadata_for_symbol(db, symbol),
        **summarize_spreads(points, range),
    }


@router.get("/spread-series")
def spread_series(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    direction: str = "long_leg_b_short_leg_a",
    range: str = "1h",
    basis: str = "entry",
) -> dict[str, Any]:
    """价差时间序列（降采样后）。"""
    safe_basis = basis if basis in {"entry", "close", "mid"} else "entry"
    points = load_spread_points(db, symbol, direction, range, basis=safe_basis)
    summary = summarize_spreads(points, range)
    return {
        "symbol": symbol.upper(),
        "direction": direction,
        "basis": safe_basis,
        **_leg_metadata_for_symbol(db, symbol),
        "range": summary["range"],
        "summary": summary,
        "items": downsample_spreads(points, range),
    }


@router.get("/venue-spreads")
def venue_spreads(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "",
    range: str = "1h",
) -> dict[str, Any]:
    """分 venue 价差分析。"""
    from app.analytics.venue_spreads import venue_spread_report
    leg_meta = _leg_metadata_for_symbol(db, symbol)
    data = venue_spread_report(
        db, symbol.upper(), range,
        leg_a_venue=leg_meta["leg_a_venue"],
        leg_b_venue=leg_meta["leg_b_venue"],
    )
    data.update(leg_meta)
    return data


@router.get("/funding-series")
def funding_series(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "BTC",
    range: str = "7d",
    bucket: str = "day",
) -> dict[str, Any]:
    """资金费率历史。"""
    return funding_history(db, symbol, range, bucket)
