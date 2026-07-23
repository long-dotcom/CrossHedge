"""
仪表盘路由
==========

- GET /dashboard/summary      —— 总览（权益、PnL、风控模式等）
- GET /dashboard/equity-curve —— 权益曲线（最近 100 个时间点）
- GET /dashboard/risk-summary —— 风控设置 + 最近 5 条风控事件
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.accounts.sync import latest_account_snapshots
from app.api.deps import as_dict
from app.auth.dependencies import get_current_user
from app.db.models import (
    AccountSnapshot,
    Alert,
    HedgeGroup,
    RiskEvent,
    RiskSetting,
)
from app.db.session import get_db
from app.execution.hedge_pool import hedge_pool
from app.execution.pnl import pnl_breakdown_from_close_spread
from app.core.time_utils import utc_now
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
    realized_pnl = float(
        db.query(func.coalesce(func.sum(HedgeGroup.realized_pnl), 0.0))
        .filter(HedgeGroup.status == "closed")
        .scalar()
        or 0.0
    )
    # 数据库时间统一保存为 naive UTC；“今日”也必须使用同一时区边界，
    # 否则历史已平仓收益会被错误地永久计入今日盈亏。
    day_start = datetime.combine(utc_now().date(), time.min)
    day_end = day_start + timedelta(days=1)
    today_realized_pnl = float(
        db.query(func.coalesce(func.sum(HedgeGroup.realized_pnl), 0.0))
        .filter(
            HedgeGroup.status == "closed",
            HedgeGroup.closed_at >= day_start,
            HedgeGroup.closed_at < day_end,
        )
        .scalar()
        or 0.0
    )
    unrealized_pnl, remaining_close_fees = _runtime_open_pnl(db)
    return {
        "equity": equity,
        "today_pnl": today_realized_pnl + unrealized_pnl,
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
    """组装权益曲线数据（最近 100 个时间点）。"""
    rows = db.query(AccountSnapshot).order_by(
        desc(AccountSnapshot.created_at), desc(AccountSnapshot.id)
    ).limit(240).all()
    rows = list(reversed(rows))
    latest_by_platform: dict[str, AccountSnapshot] = {}
    points: list[dict[str, Any]] = []
    batch: list[AccountSnapshot] = []

    def flush_batch() -> None:
        if not batch:
            return
        for snapshot in batch:
            latest_by_platform[snapshot.platform] = snapshot
        point_time = max(s.created_at for s in batch)
        points.append({
            "time": point_time.isoformat(),
            "equity": sum(s.equity for s in latest_by_platform.values()),
            "platform": "total",
            "platforms": {p: s.equity for p, s in latest_by_platform.items()},
        })

    for row in rows:
        if batch and (row.created_at - batch[-1].created_at).total_seconds() > 2:
            flush_batch()
            batch = []
        batch.append(row)
    flush_batch()
    return points[-100:]


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
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """权益曲线。"""
    return _equity_curve_payload(db)


@router.get("/risk-summary")
def risk_summary(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """风控设置 + 最近 5 条风控事件。"""
    risk = db.query(RiskSetting).first()
    latest_events = db.query(RiskEvent).order_by(desc(RiskEvent.created_at)).limit(5).all()
    return {"risk": as_dict(risk) if risk else {}, "events": [as_dict(r) for r in latest_events]}
