"""后端高频查询与 SSE 共享快照回归测试。"""

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.accounts.sync import latest_account_snapshots
from app.api import streaming
from app.api.dashboard import _dashboard_summary_payload
from app.db.models import AccountSnapshot, Base, HedgeGroup
from app.core.time_utils import utc_now


def _session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_latest_account_snapshots_returns_one_row_per_platform() -> None:
    db = _session()
    db.add_all([
        AccountSnapshot(platform="hyperliquid", equity=100, available_balance=100, margin_used=0, margin_ratio=1, currency="USD"),
        AccountSnapshot(platform="hyperliquid", equity=120, available_balance=120, margin_used=0, margin_ratio=1, currency="USD"),
        AccountSnapshot(platform="mt5", equity=80, available_balance=80, margin_used=0, margin_ratio=1, currency="USD"),
    ])
    db.commit()
    rows = latest_account_snapshots(db)
    assert {row.platform: row.equity for row in rows} == {"hyperliquid": 120, "mt5": 80}


def test_dashboard_realized_pnl_uses_closed_groups_only() -> None:
    db = _session()
    db.add_all([
        HedgeGroup(symbol="A", direction="long_leg_a_short_leg_b", status="closed", notional=1, quantity=1, realized_pnl=7),
        HedgeGroup(symbol="B", direction="long_leg_a_short_leg_b", status="open", notional=1, quantity=1, realized_pnl=99),
    ])
    db.commit()
    assert _dashboard_summary_payload(db)["realized_pnl"] == 7


def test_dashboard_today_pnl_excludes_historical_realized_pnl() -> None:
    db = _session()
    db.add_all([
        HedgeGroup(
            symbol="TODAY", direction="long_leg_a_short_leg_b", status="closed",
            notional=1, quantity=1, realized_pnl=7, closed_at=utc_now(),
        ),
        HedgeGroup(
            symbol="HISTORY", direction="long_leg_a_short_leg_b", status="closed",
            notional=1, quantity=1, realized_pnl=99, closed_at=utc_now() - timedelta(days=2),
        ),
    ])
    db.commit()

    result = _dashboard_summary_payload(db)

    assert result["realized_pnl"] == 106
    assert result["today_pnl"] == 7


def test_sse_snapshot_is_shared_within_ttl(monkeypatch) -> None:
    calls = 0

    class DummySession:
        def close(self) -> None:
            pass

    def fake_snapshot(_db, **_params):
        nonlocal calls
        calls += 1
        return {"value": calls}

    monkeypatch.setattr(streaming, "SessionLocal", DummySession)
    monkeypatch.setattr(streaming, "_stream_snapshot", fake_snapshot)
    streaming._snapshot_cache.clear()
    params = {"channel": "test-shared-cache"}
    first = streaming._cached_stream_event(10, **params)
    second = streaming._cached_stream_event(10, **params)
    assert first == second
    assert calls == 1
