"""API 端点集成测试：对冲组、持仓、账户、仪表盘、日志、风控、流式推送等接口。"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    AccountSnapshot, Alert, ArbitrageOpportunity, AuditLog, Base, Fill,
    HedgeGroup, Order, Position, RiskEvent, RiskSetting, SpreadCurrent,
    SpreadDirectionCurrent, StrategySetting, SymbolMapping, SystemLog,
    SystemSetting, User,
)
from app.api import router as api_router
from app.execution.hedge_pool import hedge_pool
from app.market.quotes import quote_cache
from app.market.scan_state import scan_state_store
from app.diagnostics.pipeline import _pool_payload
from app.execution.hedge_pool import HedgeGroupSnapshot


def test_pipeline_pool_payload_calculates_runtime_unrealized_pnl() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    group = HedgeGroup(
        id=9,
        symbol="POOL-PNL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_a_quantity=2,
        leg_b_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    quote_cache.put("hyperliquid", "POOL-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "POOL-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    item = _pool_payload([group], now)["items"][0]

    assert item["current_close_spread"] == 16
    assert item["unrealized_pnl"] == 8

def test_dashboard_stream_channel_returns_summary_and_curve() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(AccountSnapshot(platform="mt5", equity=200, available_balance=180, margin_used=20, margin_ratio=10))
    db.commit()

    event = api_router._stream_snapshot(db, channel="dashboard")

    assert set(event) == {"dashboard_summary", "equity_curve"}
    assert event["dashboard_summary"]["equity"] == 200
    assert len(event["equity_curve"]) == 1

def test_hedge_groups_stream_channel_returns_only_current_page() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add_all(
        [
            HedgeGroup(symbol="HG1", direction="long_leg_a_short_leg_b", status="open", execution_mode="paper", notional=100, quantity=1),
            HedgeGroup(symbol="HG2", direction="long_leg_a_short_leg_b", status="open", execution_mode="paper", notional=100, quantity=1),
        ]
    )
    db.commit()

    event = api_router._stream_snapshot(db, channel="hedge-groups", page=1, page_size=1)

    assert set(event) == {"hedge_groups"}
    assert event["hedge_groups"]["total"] == 2
    assert event["hedge_groups"]["page"] == 1
    assert len(event["hedge_groups"]["items"]) == 1

def test_risk_stream_channel_returns_status_and_current_event_page() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(RiskSetting(mode="paused"))
    db.add(RiskEvent(level="warning", rule="latency", message="slow", symbol="OIL"))
    db.commit()

    event = api_router._stream_snapshot(db, channel="risk", page=1, page_size=10)

    assert set(event) == {"risk_status", "risk_events"}
    assert event["risk_status"]["mode"] == "paused"
    assert event["risk_events"]["total"] == 1
    assert event["risk_events"]["items"][0]["rule"] == "latency"

def test_dashboard_summary_uses_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(AccountSnapshot(platform="hyperliquid", equity=100, available_balance=90, margin_used=10, margin_ratio=10))
    group = HedgeGroup(
        symbol="DASH-PNL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_a_quantity=2,
        leg_b_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    closed = HedgeGroup(symbol="DASH-CLOSED", direction="long_leg_a_short_leg_b", status="closed", execution_mode="paper", notional=100, quantity=1, realized_pnl=3)
    db.add_all([group, closed])
    db.commit()
    hedge_pool.load_from_db(db)
    quote_cache.put("hyperliquid", "DASH-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "DASH-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    result = api_router.dashboard_summary(User(username="admin", password_hash="x", role="admin"), db)

    assert result["equity"] == 100
    assert result["realized_pnl"] == 3
    assert result["unrealized_pnl"] == 8
    assert result["today_pnl"] == 11

def test_positions_stream_channel_returns_only_positions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=76, mark_price=77, unrealized_pnl=-2.5))
    db.commit()

    event = api_router._stream_snapshot(db, channel="positions")

    assert set(event) == {"positions"}
    assert len(event["positions"]) == 1
    assert event["positions"][0]["symbol"] == "USOIL"
    assert event["positions"][0]["unrealized_pnl"] == -2.5

def test_accounts_stream_channel_returns_only_latest_accounts() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add_all(
        [
            AccountSnapshot(platform="hyperliquid", equity=100, available_balance=90, margin_used=10, margin_ratio=10),
            AccountSnapshot(platform="mt5", equity=200, available_balance=180, margin_used=20, margin_ratio=10),
        ]
    )
    db.commit()

    event = api_router._stream_snapshot(db, channel="accounts")

    assert set(event) == {"accounts"}
    assert {item["platform"] for item in event["accounts"]} == {"hyperliquid", "mt5"}
    assert sum(item["equity"] for item in event["accounts"]) == 300

def test_execution_reconcile_api_runs_reconciler_and_audits(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.commit()
    db.refresh(user)
    monkeypatch.setattr(api_router, "run_execution_reconcile", lambda session: 3)

    result = api_router.execution_reconcile(user=user, db=db)

    assert result == {"status": "ok", "changed": 3, "cost_changed": 0}
    assert db.query(AuditLog).filter(AuditLog.action == "run_execution_reconcile", AuditLog.detail == "3").count() == 1

def test_lead_lag_stream_channel_returns_only_report() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()

    event = api_router._stream_snapshot(db, channel="lead-lag", symbol="JP225", window_seconds=60, threshold_bps=3, min_move=0, max_lag_ms=2000)

    assert set(event) == {"lead_lag"}
    assert event["lead_lag"]["symbol"] == "JP225"
    assert "summary" in event["lead_lag"]

def test_hedge_groups_api_returns_realtime_spreads() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="paper",
            notional=1000,
            quantity=1,
            entry_spread=12,
            exit_target=2,
        )
    )
    db.commit()
    quote_cache.put("hyperliquid", "OIL", bid=99, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "OIL", bid=110, ask=111, depth_notional=1000, source="test")

    result = api_router.hedge_groups(user, db, page=1, page_size=20)

    item = result["items"][0]
    assert item["entry_spread"] == 12
    assert item["current_entry_spread"] == 9
    assert item["current_close_spread"] == 12
    assert item["quote_time_diff_ms"] >= 0
    assert item["quote_age_ms"] >= 0

def test_execution_stream_channel_returns_current_order_and_fill_pages() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    order = Order(platform="hyperliquid", symbol="OIL", side="buy", quantity=1, status="filled")
    db.add(order)
    db.flush()
    db.add(Fill(order_id=order.id, platform="hyperliquid", symbol="OIL", side="buy", quantity=1, price=80, fee=0.1))
    db.commit()

    event = api_router._stream_snapshot(db, channel="execution", page=1, fill_page=1, page_size=20)

    assert set(event) == {"orders", "fills"}
    assert event["orders"]["total"] == 1
    assert event["orders"]["items"][0]["symbol"] == "OIL"
    assert event["fills"]["total"] == 1
    assert event["fills"]["items"][0]["price"] == 80

def test_close_group_api_routes_manual_intervention_to_engine(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="manual_intervention",
        execution_mode="paper",
        notional=1000,
        quantity=1.0,
    )
    db.add(user)
    db.add(group)
    db.commit()
    db.refresh(user)
    db.refresh(group)
    called = []

    def fake_engine_close(session, group_id, reason):
        called.append((group_id, reason))
        row = session.get(HedgeGroup, group_id)
        row.status = "closed"
        return row

    monkeypatch.setattr(api_router, "close_hedge_group", fake_engine_close)
    monkeypatch.setattr(api_router, "close_hedge_group_from_pool", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("manual close must not use pool path")))
    monkeypatch.setattr(api_router, "persist_hedge_pool_events", lambda db: 0)

    result = api_router.close_group(group.id, SimpleNamespace(reason="manual close", force=True), user=user, db=db)

    assert called == [(group.id, "manual close")]
    assert result["status"] == "closed"
    assert db.query(AuditLog).filter(AuditLog.action == "close_hedge_group").count() == 1

def test_hedge_groups_api_returns_runtime_unrealized_pnl() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    group = HedgeGroup(
        symbol="GROUP-PNL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_a_quantity=2,
        leg_b_quantity=2,
        entry_spread=20,
        unrealized_pnl=0,
    )
    db.add(group)
    db.commit()
    hedge_pool.load_from_db(db)
    quote_cache.put("hyperliquid", "GROUP-PNL", bid=100, ask=101, depth_notional=1000, source="test")
    quote_cache.put("mt5", "GROUP-PNL", bid=115, ask=116, depth_notional=1000, source="test")

    result = api_router.hedge_groups(user, db, page=1, page_size=20)

    item = result["items"][0]
    assert item["current_close_spread"] == 16
    assert item["unrealized_pnl"] == 8

def test_pipeline_pool_payload_accepts_hedge_group_snapshot() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    snapshot = HedgeGroupSnapshot(
        id=10,
        symbol="POOL-SNAPSHOT",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="paper",
        notional=1000,
        quantity=1,
        leg_b_quantity=1,
        leg_a_quantity=1,
        open_cost=0,
        fees=0,
        funding=0,
        swap=0,
        realized_pnl=0,
        unrealized_pnl=0,
        trigger_spread=20,
        entry_spread=20,
        entry_threshold=20,
        exit_target=10,
        overheat_threshold=0,
        close_reason="",
        opened_at=now,
        closed_at=None,
        source="auto_paper",
    )

    item = _pool_payload([snapshot], now)["items"][0]

    assert item["id"] == 10
    assert item["age_ms"] == 0

def test_spread_and_opportunity_apis_prefer_memory_scan_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="BTC", leg_a_venue_symbol="BTC", mt5_symbol="BTCUSD", enabled=True))
    db.add(SymbolMapping(symbol="ETH", leg_a_venue_symbol="ETH", mt5_symbol="ETHUSD", enabled=True))
    db.commit()
    scan_state_store.update(
        [
            {
                "id": 10,
                "symbol": "BTC",
                "direction": "long_leg_b_short_leg_a",
                "leg_a_bid": 100.0,
            }
        ],
        [
            {
                "id": 20,
                "symbol": "ETH",
                "direction": "long_leg_a_short_leg_b",
                "status": "candidate",
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            }
        ],
    )

    spreads_payload = api_router.spreads(SimpleNamespace(), db, page=1, page_size=20)
    opportunities_payload = api_router.opportunities(SimpleNamespace(), db, page=1, page_size=20)

    assert spreads_payload["items"][0]["symbol"] == "BTC"
    assert opportunities_payload["items"][0]["symbol"] == "ETH"

def test_equity_curve_aggregates_platform_snapshots_by_sync_batch() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    first = datetime(2026, 6, 25, 3, 12, 0)
    second = first + timedelta(minutes=5)
    db.add_all(
        [
            AccountSnapshot(platform="hyperliquid", equity=0, available_balance=0, margin_used=0, margin_ratio=1, created_at=first),
            AccountSnapshot(platform="mt5", equity=50000, available_balance=49000, margin_used=1000, margin_ratio=50, created_at=first + timedelta(milliseconds=300)),
            AccountSnapshot(platform="hyperliquid", equity=100, available_balance=100, margin_used=0, margin_ratio=1, created_at=second),
            AccountSnapshot(platform="mt5", equity=49900, available_balance=48900, margin_used=1000, margin_ratio=50, created_at=second + timedelta(milliseconds=300)),
        ]
    )
    db.commit()

    curve = api_router._equity_curve_payload(db)

    assert [point["platform"] for point in curve] == ["total", "total"]
    assert [point["equity"] for point in curve] == [50000, 50000]
    assert curve[0]["platforms"] == {"hyperliquid": 0, "mt5": 50000}

def test_pipeline_pool_payload_uses_stable_stage_symbol_id_order() -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    groups = [
        HedgeGroup(id=5, symbol="ZINC", direction="long_leg_b_short_leg_a", status="manual_intervention", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=3, symbol="OIL", direction="long_leg_b_short_leg_a", status="open", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=2, symbol="EUR", direction="long_leg_b_short_leg_a", status="opening", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=1, symbol="BTC", direction="long_leg_b_short_leg_a", status="pending_open", execution_mode="paper", notional=1, quantity=1),
        HedgeGroup(id=4, symbol="BTC", direction="long_leg_b_short_leg_a", status="closing", execution_mode="paper", notional=1, quantity=1),
    ]

    items = _pool_payload(groups, now)["items"]

    assert [(item["stage"], item["symbol"], item["id"]) for item in items] == [
        ("pending", "BTC", 1),
        ("opening", "EUR", 2),
        ("open", "OIL", 3),
        ("closing", "BTC", 4),
        ("manual", "ZINC", 5),
    ]

def test_logs_stream_channel_returns_current_log_and_alert_pages() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SystemLog(level="info", category="test", message="hello"))
    db.add(Alert(level="critical", title="risk", message="check"))
    db.commit()

    event = api_router._stream_snapshot(db, channel="logs", page=1, alert_page=1, page_size=20)

    assert set(event) == {"logs", "alerts"}
    assert event["logs"]["total"] == 1
    assert event["logs"]["items"][0]["message"] == "hello"
    assert event["alerts"]["total"] == 1
    assert event["alerts"]["items"][0]["title"] == "risk"
