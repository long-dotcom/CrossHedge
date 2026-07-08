"""
分析路由
========

- GET /analytics/spread-summary  —— 价差汇总统计
- GET /analytics/spread-series   —— 价差时间序列
- GET /analytics/venue-spreads   —— 分 venue 价差分析
- GET /analytics/funding-series  —— 资金费率历史
- GET /analytics/lead-lag        —— 领先-滞后分析
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.analytics.funding import funding_history
from app.analytics.lead_lag import lead_lag_report
from app.analytics.spreads import downsample_spreads, load_spread_points, summarize_spreads
from app.api.deps import _leg_metadata, _leg_metadata_for_symbol
from app.auth.dependencies import get_current_user
from app.db.models import SymbolMapping, User
from app.db.session import get_db

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _lead_lag_payload(
    db: Session,
    symbol: str,
    window_seconds: int,
    threshold_bps: float,
    min_move: float,
    follow_ratio: float,
    max_lag_ms: int,
) -> dict[str, Any]:
    """组装领先-滞后分析报告。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol.upper()).first()
    leg_meta = _leg_metadata(mapping)
    data = lead_lag_report(
        symbol,
        window_seconds,
        threshold_bps,
        min_move,
        follow_ratio,
        max_lag_ms,
        leg_a_venue=leg_meta["leg_a_venue"],
        leg_b_venue=leg_meta["leg_b_venue"],
    )
    data.update(leg_meta)
    return data


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


@router.get("/lead-lag")
def lead_lag(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    symbol: str = "JP225",
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
) -> dict[str, Any]:
    """领先-滞后分析报告。"""
    return _lead_lag_payload(db, symbol, window_seconds, threshold_bps, min_move, follow_ratio, max_lag_ms)
