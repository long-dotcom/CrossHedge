"""组合权益历史的写入、历史回填和分粒度查询。"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import AccountSnapshot, PortfolioEquitySnapshot

RANGE_DURATIONS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
RANGE_BUCKET_SECONDS = {
    "24h": 60,
    "7d": 5 * 60,
    "30d": 30 * 60,
}
ALL_RANGE_BUCKETS = (60, 300, 900, 1800, 3600, 14400, 86400, 604800, 2592000)
ALL_RANGE_TARGET_POINTS = 1200
UTC_EPOCH = datetime(1970, 1, 1)


def record_portfolio_equity_snapshot(
    db: Session,
    current_snapshots: Iterable[AccountSnapshot],
) -> PortfolioEquitySnapshot:
    """将一轮平台快照合并为一个组合权益点，失败平台沿用最后有效值。"""
    current = list(current_snapshots)
    valid = {row.platform: float(row.equity) for row in current if not _is_error_snapshot(row)}
    failed = sorted({row.platform for row in current if _is_error_snapshot(row)})
    missing: list[str] = []
    equities = dict(valid)
    for platform in failed:
        fallback = _latest_valid_platform_equity(db, platform)
        if fallback is None:
            missing.append(platform)
        else:
            equities[platform] = fallback
    quality = "complete" if not failed else ("invalid" if missing else "partial")
    row = _portfolio_row(
        equities,
        quality=quality,
        missing=missing,
        created_at=max((item.created_at for item in current if item.created_at), default=utc_now()),
    )
    db.add(row)
    return row


def backfill_portfolio_equity_history(db: Session) -> int:
    """首次升级时把现有账户快照按同步批次回填到组合权益历史表。"""
    if db.query(PortfolioEquitySnapshot.id).first() is not None:
        return 0
    rows = db.query(AccountSnapshot).order_by(AccountSnapshot.created_at, AccountSnapshot.id).yield_per(2000)
    latest: dict[str, float] = {}
    batch: list[AccountSnapshot] = []
    inserted = 0

    def flush() -> None:
        nonlocal inserted
        if not batch:
            return
        batch_platforms = {row.platform for row in batch}
        failed = {row.platform for row in batch if _is_error_snapshot(row)}
        for row in batch:
            if not _is_error_snapshot(row):
                latest[row.platform] = float(row.equity)
        equities = {platform: latest[platform] for platform in batch_platforms if platform in latest}
        missing = sorted(platform for platform in batch_platforms if platform not in latest)
        quality = "complete" if not failed else ("invalid" if missing else "partial")
        db.add(_portfolio_row(
            equities,
            quality=quality,
            missing=missing,
            created_at=max(row.created_at for row in batch),
        ))
        inserted += 1
        if inserted % 1000 == 0:
            db.flush()

    for row in rows:
        if batch and (row.created_at - batch[-1].created_at).total_seconds() > 2:
            flush()
            batch = []
        batch.append(row)
    flush()
    db.commit()
    return inserted


def equity_curve_points(
    db: Session,
    time_range: str = "24h",
    *,
    now: datetime | None = None,
) -> list[dict]:
    """按范围和对应时间桶返回完整覆盖的组合权益曲线。"""
    normalized = time_range if time_range in RANGE_DURATIONS else "24h"
    current = now or utc_now()
    duration = RANGE_DURATIONS[normalized]
    start_at = current - duration if duration else None
    query = db.query(PortfolioEquitySnapshot)
    if start_at is not None:
        query = query.filter(PortfolioEquitySnapshot.created_at >= start_at)
    bounds = query.with_entities(
        func.min(PortfolioEquitySnapshot.created_at),
        func.max(PortfolioEquitySnapshot.created_at),
    ).one()
    if not bounds[0] or not bounds[1]:
        return []
    bucket_seconds = RANGE_BUCKET_SECONDS.get(normalized) or _all_range_bucket_seconds(bounds[0], bounds[1])
    rows = query.order_by(PortfolioEquitySnapshot.created_at, PortfolioEquitySnapshot.id).yield_per(2000)
    selected_rows: list[PortfolioEquitySnapshot] = []
    current_bucket: int | None = None
    first: PortfolioEquitySnapshot | None = None
    selected: PortfolioEquitySnapshot | None = None
    for row in rows:
        if first is None:
            first = row
        # 数据库存储 naive UTC，不能使用受服务器本地时区影响的 datetime.timestamp()。
        bucket = int((row.created_at - UTC_EPOCH).total_seconds()) // bucket_seconds
        if current_bucket is not None and bucket != current_bucket and selected is not None:
            selected_rows.append(selected)
        current_bucket = bucket
        selected = row
    if selected is not None:
        selected_rows.append(selected)
    # “全部”必须保留数据库中的第一个真实点，而不是只保留首个时间桶的末值。
    if normalized == "all" and first is not None and selected_rows and selected_rows[0].id != first.id:
        selected_rows.insert(0, first)
    return [_point_payload(row, bucket_seconds) for row in selected_rows]


def _portfolio_row(
    equities: dict[str, float],
    *,
    quality: str,
    missing: list[str],
    created_at: datetime,
) -> PortfolioEquitySnapshot:
    ordered = {platform: equities[platform] for platform in sorted(equities)}
    total = sum(ordered.values()) if ordered and quality != "invalid" else None
    return PortfolioEquitySnapshot(
        total_equity=total,
        platform_equities=json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        data_quality=quality,
        missing_platforms=json.dumps(sorted(missing), ensure_ascii=False, separators=(",", ":")),
        created_at=created_at,
        updated_at=created_at,
    )


def _point_payload(row: PortfolioEquitySnapshot, bucket_seconds: int) -> dict:
    return {
        "time": row.created_at.isoformat(),
        "equity": row.total_equity,
        "platform": "total",
        "platforms": json.loads(row.platform_equities or "{}"),
        "quality": row.data_quality,
        "missing_platforms": json.loads(row.missing_platforms or "[]"),
        "bucket_seconds": bucket_seconds,
    }


def _latest_valid_platform_equity(db: Session, platform: str) -> float | None:
    row = (
        db.query(AccountSnapshot)
        .filter(
            AccountSnapshot.platform == platform,
            ~AccountSnapshot.data_source.like("%_error"),
        )
        .order_by(AccountSnapshot.created_at.desc(), AccountSnapshot.id.desc())
        .first()
    )
    return float(row.equity) if row else None


def _is_error_snapshot(row: AccountSnapshot) -> bool:
    return str(row.data_source or "").endswith("_error")


def _all_range_bucket_seconds(first: datetime, last: datetime) -> int:
    required = max(math.ceil(max((last - first).total_seconds(), 1) / ALL_RANGE_TARGET_POINTS), 60)
    return next((value for value in ALL_RANGE_BUCKETS if value >= required), ALL_RANGE_BUCKETS[-1])
