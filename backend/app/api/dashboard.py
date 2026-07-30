"""
仪表盘路由
==========

- GET /dashboard/summary      —— 总览（权益、PnL、风控模式等）
- GET /dashboard/equity-curve —— 按范围和时间粒度查询持久化权益曲线
- GET /dashboard/risk-summary —— 风控设置 + 最近 5 条风控事件
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.accounts.sync import latest_account_snapshots
from app.accounts.equity_history import equity_curve_points
from app.api.deps import as_dict
from app.auth.dependencies import get_current_user
from app.db.models import (
    Alert,
    HedgeGroup,
    RiskEvent,
    RiskSetting,
)
from app.db.session import get_db
from app.execution.hedge_pool import hedge_pool
from app.execution.pnl import pnl_breakdown_from_close_spread, realized_pnl_from_fills
from app.core.time_utils import utc_now
from app.config.settings import get_settings
from app.market.hedge_spreads import hedge_group_spreads
from app.db.models import User

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助：开放对冲组未实现盈亏
# ---------------------------------------------------------------------------

def _runtime_open_pnl(db: Session) -> tuple[float, float]:
    """返回当前立即平仓净 PnL及其中尚未发生的预计平仓手续费。"""
    groups = (
        db.query(HedgeGroup)
        .filter(HedgeGroup.status.in_(["open", "open_partial"]))
        .order_by(HedgeGroup.id.asc())
        .all()
    )
    active_by_id = {s.id: s for s in hedge_pool.snapshot_groups()}
    total = 0.0
    remaining_close_fees = 0.0
    for row in groups:
        group = active_by_id.get(row.id)
        group = group if group and group.symbol == row.symbol else row
        spreads = hedge_group_spreads(group)
        current_close_spread = spreads.get("current_close_spread")
        if current_close_spread is None:
            total += float(group.unrealized_pnl or 0.0)
            remaining_close_fees += float(getattr(group, "estimated_close_fee", 0.0) or 0.0)
            continue
        try:
            pnl = pnl_breakdown_from_close_spread(
                group, float(current_close_spread), include_estimated_close_fee=True,
            )
            total += pnl.net_pnl
            remaining_close_fees += pnl.estimated_close_fee
        except (TypeError, ValueError):
            total += float(group.unrealized_pnl or 0.0)
    return total, remaining_close_fees


def _runtime_open_unrealized_pnl(db: Session) -> float:
    """兼容旧调用名：返回当前立即平仓后的净 PnL。"""
    return _runtime_open_pnl(db)[0]


def _local_day_utc_bounds(
    now_utc: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[datetime, datetime]:
    """把业务时区的本地自然日换算为数据库使用的 naive UTC 边界。"""
    name = timezone_name or get_settings().app_timezone
    try:
        local_tz = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        local_tz = timezone.utc
    current = now_utc or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    local_date = current.astimezone(local_tz).date()
    local_start = datetime.combine(local_date, time.min, tzinfo=local_tz)
    start_utc = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


# ---------------------------------------------------------------------------
# 内部辅助：仪表盘摘要
# ---------------------------------------------------------------------------

def _dashboard_summary_payload(db: Session) -> dict[str, Any]:
    """组装仪表盘摘要数据。"""
    latest_accounts = latest_account_snapshots(db)
    equity = sum(row.equity for row in latest_accounts)
    open_groups = db.query(HedgeGroup).filter(
        HedgeGroup.status.in_(["opening", "open", "open_partial", "closing", "manual_intervention"])
    ).count()
    alerts = db.query(Alert).filter(Alert.acknowledged.is_(False)).count()
    risk = db.query(RiskSetting).first()
    closed_groups = db.query(HedgeGroup).filter(HedgeGroup.status == "closed").all()
    realized_by_group: dict[int, float] = {}
    for group in closed_groups:
        calculated = realized_pnl_from_fills(db, group)
        realized_by_group[group.id] = (
            calculated if calculated is not None else float(group.realized_pnl or 0.0)
        )
    realized_pnl = sum(realized_by_group.values())
    # 数据库保存 naive UTC，但“今日”是业务时区的本地自然日。
    day_start, day_end = _local_day_utc_bounds()
    today_realized_pnl = sum(
        realized_by_group[group.id]
        for group in closed_groups
        if group.closed_at is not None and day_start <= group.closed_at < day_end
    )
    unrealized_pnl, remaining_close_fees = _runtime_open_pnl(db)
    return {
        "equity": equity,
        "today_pnl": today_realized_pnl + unrealized_pnl,
        "today_realized_pnl": today_realized_pnl,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "remaining_close_fees": remaining_close_fees,
        "pnl_basis": "liquidation",
        "risk_mode": risk.mode if risk else "normal",
        "open_hedge_groups": open_groups,
        "unread_alerts": alerts,
    }


# ---------------------------------------------------------------------------
# 内部辅助：权益曲线
# ---------------------------------------------------------------------------

def _equity_curve_payload(db: Session) -> list[dict[str, Any]]:
    """兼容 SSE 内部调用的默认 24 小时权益曲线。"""
    return equity_curve_points(db, "24h")


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("/summary")
def dashboard_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """仪表盘总览。"""
    return _dashboard_summary_payload(db)


@router.get("/equity-curve")
def equity_curve(
    time_range: Literal["24h", "7d", "30d", "all"] = Query("24h", alias="range"),
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """按时间范围和对应粒度返回持久化权益曲线。"""
    return equity_curve_points(db, time_range)


@router.get("/risk-summary")
def risk_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """风控设置 + 最近 5 条风控事件。"""
    risk = db.query(RiskSetting).first()
    latest_events = db.query(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(5).all()
    return {"risk": as_dict(risk) if risk else {}, "events": [as_dict(r) for r in latest_events]}
