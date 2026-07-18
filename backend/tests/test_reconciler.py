"""执行对账边界测试：只回查和告警，绝不绕过 Coordinator 直接交易。"""

from types import SimpleNamespace
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Alert,
    Base,
    ExecutionIntent,
    HedgeGroup,
    HedgeGroupEvent,
    Order,
    Position,
)
from app.execution.reconciler import (
    reconcile_hedge_group,
    reconcile_orphan_positions,
    reconcile_residual_positions,
    sync_live_positions,
)
from tests.conftest import _pending_reconcile_test_db
from tests.native_fakes import order_snapshot
from app.venues.domain.models import Position as VenuePosition, PositionSide


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_new_intent_group_is_not_touched_by_legacy_reconciler(monkeypatch) -> None:
    """新执行模型完全由事件投影推进，旧对账器连查询网关都不应发生。"""
    db, group = _pending_reconcile_test_db("opening")
    db.add(ExecutionIntent(
        hedge_group_id=group.id,
        intent_type="OPEN",
        execution_mode="live",
        idempotency_key="open-boundary-test",
    ))
    db.commit()
    assert reconcile_hedge_group(db, group) is False
    assert group.status == "opening"


def test_legacy_single_fill_escalates_without_submit_or_cancel(monkeypatch) -> None:
    """历史单边成交只能升级人工恢复，不可由对账器私自撤单或反向补单。"""
    db, group = _pending_reconcile_test_db("opening")

    class QueryOnlyConnector:
        def __init__(self, venue):
            self.venue = venue

        def get_open_orders(self):
            return []

        def get_order(self, symbol, **kwargs):
            if self.venue == "hyperliquid":
                return order_snapshot(venue=self.venue, symbol=symbol, requested=1, filled=1, price=100, commission=0.1)
            return order_snapshot(venue=self.venue, symbol=symbol, status="accepted", requested=1)

        def get_fills(self, *args, **kwargs):
            return []

    monkeypatch.setattr(
        "app.execution.reconciler.native_venue_manager.connector_for",
        lambda venue, mode: QueryOnlyConnector(venue),
    )

    assert reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)
    pending = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert group.status == "manual_intervention"
    assert pending.status == "accepted"
    assert db.query(Order).filter(Order.hedge_group_id == group.id).count() == 2
    assert db.query(HedgeGroupEvent).filter(
        HedgeGroupEvent.hedge_group_id == group.id,
        HedgeGroupEvent.event_type == "manual_intervention",
    ).count() == 1


def test_failed_position_snapshot_preserves_last_known_rows(monkeypatch) -> None:
    """平台断线时保留最后已知仓位，不能把未知误判为空仓。"""
    db = _db()
    db.add(Position(
        platform="hyperliquid", symbol="OIL", side="long", quantity=1.5,
        entry_price=76.0, mark_price=76.1, unrealized_pnl=0.15,
    ))
    db.commit()

    class Connector:
        def __init__(self, venue):
            self.venue = venue

        def get_positions(self):
            if self.venue == "hyperliquid":
                raise ConnectionError("断线")
            return []

    monkeypatch.setattr("app.execution.reconciler.native_venue_manager.connector_for", lambda venue, mode: Connector(venue))

    assert sync_live_positions(db) == 0
    db.commit()
    row = db.query(Position).filter(Position.platform == "hyperliquid").one()
    assert row.quantity == 1.5


def test_successful_position_snapshot_atomically_replaces_platform_rows(monkeypatch) -> None:
    db = _db()
    db.add(Position(
        platform="hyperliquid", symbol="OLD", side="long", quantity=1.0,
        entry_price=1.0, mark_price=1.0,
    ))
    db.commit()
    position = VenuePosition(
        venue="hyperliquid", account_id="test", symbol="OIL", position_side=PositionSide.SHORT,
        quantity=Decimal("2"), entry_price=Decimal("76"), mark_price=Decimal("75"),
    )
    class Connector:
        def __init__(self, venue):
            self.venue = venue

        def get_positions(self):
            return [position] if self.venue == "hyperliquid" else []

    monkeypatch.setattr("app.execution.reconciler.native_venue_manager.connector_for", lambda venue, mode: Connector(venue))

    assert sync_live_positions(db) == 1
    db.commit()
    rows = db.query(Position).filter(Position.platform == "hyperliquid").all()
    assert [(row.symbol, row.side, row.quantity) for row in rows] == [("OIL", "short", 2.0)]


def test_closed_live_group_with_residual_position_is_escalated() -> None:
    db = _db()
    group = HedgeGroup(
        symbol="OIL", direction="long_leg_a_short_leg_b", status="closed",
        execution_mode="live", notional=1000, quantity=1.0,
    )
    db.add(group)
    db.add(Position(
        platform="hyperliquid", symbol="OIL", side="long", quantity=0.25,
        entry_price=76.0, mark_price=76.0,
    ))
    db.commit()

    assert reconcile_residual_positions(db) == 1
    assert group.status == "manual_intervention"
    assert db.query(Alert).filter(Alert.title == "平仓后残余仓位").count() == 1


def test_unmanaged_native_position_creates_single_idempotent_alert() -> None:
    db = _db()
    db.add(Position(
        platform="hyperliquid", symbol="OIL", side="short", quantity=0.5,
        entry_price=76.0, mark_price=75.0,
    ))
    db.commit()

    assert reconcile_orphan_positions(db) == 1
    db.flush()
    assert reconcile_orphan_positions(db) == 0
    assert db.query(Alert).filter(Alert.title == "外部孤儿仓位").count() == 1
