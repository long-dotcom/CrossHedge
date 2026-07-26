"""组合权益历史持久化、回退与分粒度查询测试。"""

import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.accounts.equity_history import equity_curve_points, record_portfolio_equity_snapshot
from app.db.models import AccountSnapshot, Base, PortfolioEquitySnapshot


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True, autoflush=False)()


def test_failed_platform_uses_last_valid_equity_without_zero_drop() -> None:
    db = _db()
    previous = datetime(2026, 7, 20, 12, 0, 0)
    db.add(AccountSnapshot(
        platform="binance",
        equity=1250,
        available_balance=1200,
        margin_used=50,
        data_source="binance_native",
        created_at=previous,
    ))
    db.commit()
    current = AccountSnapshot(
        platform="binance",
        equity=0,
        available_balance=0,
        margin_used=0,
        data_source="configured_live_error",
        created_at=previous + timedelta(minutes=1),
    )

    row = record_portfolio_equity_snapshot(db, [current])
    db.commit()

    assert row.total_equity == 1250
    assert row.data_quality == "partial"
    assert json.loads(row.platform_equities) == {"binance": 1250}
    assert json.loads(row.missing_platforms) == []


def test_equity_ranges_use_different_time_granularity() -> None:
    db = _db()
    start = datetime(2026, 7, 1, 0, 0, 0)
    rows = []
    for minute in range(8 * 24 * 60 + 1):
        captured_at = start + timedelta(minutes=minute)
        rows.append(PortfolioEquitySnapshot(
            total_equity=50_000 + minute,
            platform_equities='{"mt5":50000}',
            data_quality="complete",
            missing_platforms="[]",
            created_at=captured_at,
            updated_at=captured_at,
        ))
    db.bulk_save_objects(rows)
    db.commit()
    now = start + timedelta(days=8)

    day = equity_curve_points(db, "24h", now=now)
    week = equity_curve_points(db, "7d", now=now)
    month = equity_curve_points(db, "30d", now=now)
    entire = equity_curve_points(db, "all", now=now)

    assert {point["bucket_seconds"] for point in day} == {60}
    assert {point["bucket_seconds"] for point in week} == {300}
    assert {point["bucket_seconds"] for point in month} == {1800}
    assert {point["bucket_seconds"] for point in entire} == {900}
    assert day[0]["time"].startswith("2026-07-08")
    assert entire[0]["time"] == "2026-07-01T00:00:00"
    assert entire[-1]["time"].startswith("2026-07-09")
    assert len(week) > len(day) > len(entire) > len(month)


def test_invalid_snapshot_is_returned_as_chart_gap() -> None:
    db = _db()
    captured_at = datetime(2026, 7, 20, 12, 0, 0)
    current = [
        AccountSnapshot(
            platform="binance",
            equity=0,
            available_balance=0,
            margin_used=0,
            data_source="configured_live_error",
            created_at=captured_at,
        ),
        AccountSnapshot(
            platform="mt5",
            equity=50_000,
            available_balance=49_000,
            margin_used=1_000,
            data_source="mt5_native",
            created_at=captured_at,
        ),
    ]
    record_portfolio_equity_snapshot(db, current)
    db.commit()

    points = equity_curve_points(db, "all")

    assert points[0]["equity"] is None
    assert points[0]["quality"] == "invalid"
    assert points[0]["missing_platforms"] == ["binance"]
