"""对账测试：订单对账、单边成交补偿、残余仓位检测、孤儿仓位告警、持仓同步。"""

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Alert, Base, Fill, HedgeGroup, HedgeGroupEvent, Order, Position,
    SymbolMapping,
)
from app.execution.reconciler import (
    reconcile_hedge_group, reconcile_orphan_positions,
    reconcile_residual_positions, sync_live_positions,
)
from app.execution.gateway import (
    AdapterExecutionGateway, FillEvent, GatewayOrderResult,
    LegOrderIntent, OrderEvent, build_execution_gateway,
)
from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.market.quotes import quote_cache
from app.market.mt5_sessions import MT5SessionState
from app.schemas import AdoptPositionIn
from app.api import router as api_router
from app.db.models import User, AuditLog
from conftest import _pending_reconcile_test_db


def test_reconcile_recovers_hyperliquid_fill_from_account_snapshot(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_account_orders(self, platform):
            return [
                {
                    "status": "filled",
                    "external_order_id": "hl-1",
                    "symbol": "OIL",
                    "side": "buy",
                    "quantity": 1.0,
                    "filled_quantity": 1.0,
                    "average_price": 76.5,
                    "fee": 0.2,
                    "message": "account snapshot",
                }
            ]

        def query_order(self, platform, external_order_id):
            if platform == "mt5":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.1, "average_price": 76.6, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    hl_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "hyperliquid").one()
    assert changed
    assert group.status == "open"
    assert hl_order.status == "filled"
    assert hl_order.price == 76.5
    assert db.query(Fill).filter(Fill.order_id == hl_order.id).one().fee == 0.2

def test_reconcile_opening_single_fill_auto_reverses_filled_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", single_leg_action="auto_close"))
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

        def submit_order(self, intent, *, paper_latency_ms=0):
            assert intent.platform == "hyperliquid"
            assert intent.side == "sell"
            assert intent.reduce_only is True
            result = AdapterOrderResult(True, "hl-comp", "filled", intent.quantity, 99.5, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 99.5, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 99.5, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    compensation = db.query(Order).filter(Order.hedge_group_id == group.id, Order.external_order_id == "hl-comp").one()
    assert changed
    assert group.status == "failed"
    assert group.fees == pytest.approx(0.4)
    assert compensation.side == "sell"
    assert compensation.reduce_only is True
    assert db.query(Fill).filter(Fill.order_id == compensation.id).count() == 1
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "opening_single_leg_compensation").count() == 1

def test_reconcile_opening_single_fill_cancels_pending_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "manual_intervention"
    assert mt5_order.status == "canceled"
    assert "撤销未成交腿" in group.events[-1].detail

def test_reconcile_recovers_missing_hyperliquid_external_id_from_unique_account_snapshot(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    hl_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "hyperliquid").one()
    hl_order.external_order_id = ""
    db.commit()

    class FakeGateway:
        def query_account_orders(self, platform):
            return [
                {
                    "status": "filled",
                    "external_order_id": "98765",
                    "symbol": "OIL",
                    "side": "buy",
                    "quantity": 1.0,
                    "filled_quantity": 1.0,
                    "average_price": 76.5,
                    "fee": 0.2,
                    "timestamp_ms": int(time.time() * 1000),
                    "message": "account snapshot",
                }
            ]

        def query_order(self, platform, external_order_id):
            if platform == "mt5":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.1, "average_price": 76.6, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(hl_order)

    assert changed
    assert hl_order.external_order_id == "98765"
    assert hl_order.status == "filled"
    assert db.query(Fill).filter(Fill.order_id == hl_order.id).count() == 1

def test_reconcile_closing_single_fill_auto_reverses_and_restores_open(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", single_leg_action="auto_close"))
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

        def submit_order(self, intent, *, paper_latency_ms=0):
            assert intent.platform == "hyperliquid"
            assert intent.side == "buy"
            assert intent.reduce_only is True
            result = AdapterOrderResult(True, "hl-comp", "filled", intent.quantity, 100.5, 0.1)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 100.5, 0.1)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 100.5, 0.1, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    compensation = db.query(Order).filter(Order.hedge_group_id == group.id, Order.external_order_id == "hl-comp").one()
    assert changed
    assert group.status == "open"
    assert group.fees == pytest.approx(0.4)
    assert compensation.side == "buy"
    assert compensation.reduce_only is True
    assert db.query(Fill).filter(Fill.order_id == compensation.id).count() == 1
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "closing_single_leg_compensation").count() == 1

def test_reconcile_closing_single_fill_cancels_pending_leg(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            if platform == "hyperliquid":
                return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}
            return {"status": "accepted", "external_order_id": external_order_id, "filled_quantity": 0.0}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "manual_intervention"
    assert mt5_order.status == "canceled"
    assert "撤销未成交腿" in group.close_reason

def test_adopt_position_creates_live_manual_group() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    user = User(username="admin", password_hash="x", role="admin")
    db.add(user)
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    position = Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=76, mark_price=77, unrealized_pnl=-2.5)
    db.add(position)
    db.commit()
    db.refresh(user)
    db.refresh(position)

    group = api_router.adopt_position(position.id, AdoptPositionIn(reason="import broker position"), user=user, db=db)

    assert group["status"] == "manual_intervention"
    assert group["execution_mode"] == "live"
    assert group["symbol"] == "OIL"
    assert group["direction"] == "long_leg_a_short_leg_b"
    assert group["leg_a_quantity"] == 0.0
    assert group["leg_b_quantity"] == 0.2
    assert db.query(HedgeGroupEvent).filter(HedgeGroupEvent.event_type == "adopted_external_position").count() == 1
    assert db.query(AuditLog).filter(AuditLog.action == "adopt_position").count() == 1

def test_hyperliquid_live_position_sync_triggers_residual_reconcile(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"assetPositions": [{"position": {"coin": "OIL", "szi": "-1.5", "entryPx": "76", "markPx": "75.8"}}]}).encode("utf-8")

    class FakeMT5Adapter:
        platform = "mt5"

        def __init__(self, live=False):
            pass

        def get_positions(self):
            return []

    monkeypatch.setattr("app.adapters.hyperliquid.request.urlopen", lambda req, timeout: FakeResponse())
    monkeypatch.setattr("app.adapters.hyperliquid.get_settings", lambda: SimpleNamespace(
        hyperliquid_account_address="0xabc",
        hyperliquid_info_url="https://example.test/info",
    ))
    monkeypatch.setattr("app.execution.reconciler.MT5Adapter", FakeMT5Adapter)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="closed",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        closed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()

    assert sync_live_positions(db) == 1
    changed = reconcile_residual_positions(db)
    db.commit()
    db.refresh(group)

    assert changed == 1
    assert group.status == "manual_intervention"
    assert db.query(Position).filter(Position.platform == "hyperliquid", Position.symbol == "OIL", Position.side == "short").count() == 1

def test_reconcile_opening_group_advances_to_open(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 100.0, "fee": 0.1}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "open"
    assert db.query(Fill).count() == 2

def test_reconcile_taker_open_hyper_fill_submits_mt5_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225z"))
    group = HedgeGroup(
        symbol="JP225",
        direction="long_leg_a_short_leg_b",
        status="opening",
        execution_mode="paper",
        notional=450.0,
        quantity=1.0,
        leg_a_quantity=0.00625,
        leg_b_quantity=1.0,
        open_cost=0.2,
    )
    db.add(group)
    db.flush()
    db.add(
        Order(
            hedge_group_id=group.id,
            platform="hyperliquid",
            symbol="JP225",
            side="buy",
            quantity=0.00625,
            status="accepted",
            external_order_id="hl-open-1",
        )
    )
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 0.003125, "average_price": 72000.0, "fee": 0.1}

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-open-1", "filled", intent.quantity, 72010.0, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 72010.0, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 72010.0, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "open"
    assert mt5_order.side == "sell"
    assert mt5_order.quantity == pytest.approx(0.5)
    assert mt5_order.reduce_only is False
    assert submitted[0].venue_symbol == "JP225z"
    assert db.query(Fill).count() == 2

def test_reconcile_residual_positions_marks_closed_group_manual() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="closed",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        open_cost=1.0,
        fees=0.2,
        unrealized_pnl=0.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        closed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.flush()
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.1, entry_price=80, mark_price=79))
    db.commit()

    changed = reconcile_residual_positions(db)
    db.commit()
    db.refresh(group)

    assert changed == 1
    assert group.status == "manual_intervention"
    assert "USOIL" in group.close_reason
    assert db.query(Alert).filter(Alert.title == "平仓后残余仓位").count() == 1

def test_reconcile_orphan_positions_requires_matching_side_and_quantity() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL", mt5_volume_step=0.01, mt5_contract_size=100, enabled=True))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1,
            leg_a_quantity=1.0,
            leg_b_quantity=0.1,
        )
    )
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.2, entry_price=70, mark_price=71))
    db.add(Position(platform="hyperliquid", symbol="OIL", side="short", quantity=1.0, entry_price=70, mark_price=71))
    db.commit()

    changed = reconcile_orphan_positions(db)

    assert changed == 2
    messages = [row.message for row in db.query(Alert).filter(Alert.title == "外部孤儿仓位").all()]
    assert any("mt5:USOIL:short:0.2" in message for message in messages)
    assert any("hyperliquid:OIL:short:1.0" in message for message in messages)

def test_reconcile_taker_close_hyper_fill_submits_mt5_reduce_only_leg(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="closing",
        execution_mode="paper",
        notional=1000.0,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        open_cost=0.2,
        unrealized_pnl=2.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.flush()
    db.add(
        Order(
            hedge_group_id=group.id,
            platform="hyperliquid",
            symbol="OIL",
            side="sell",
            quantity=1.0,
            reduce_only=True,
            status="accepted",
            external_order_id="hl-close-1",
        )
    )
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": 1.0, "average_price": 75.0, "fee": 0.1}

        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-close-1", "filled", intent.quantity, 75.1, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 75.1, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 75.1, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "closed"
    assert group.closed_at is not None
    assert group.unrealized_pnl == 0.0
    assert mt5_order.side == "buy"
    assert mt5_order.quantity == pytest.approx(0.1)
    assert mt5_order.reduce_only is True
    assert submitted[0].venue_symbol == "USOIL"
    assert submitted[0].reduce_only is True

def test_reconcile_orphan_positions_ignores_position_with_live_group() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="OIL", leg_a_venue_symbol="OIL", mt5_symbol="USOIL"))
    db.add(
        HedgeGroup(
            symbol="OIL",
            direction="long_leg_a_short_leg_b",
            status="open",
            execution_mode="live",
            notional=1000,
            quantity=1.0,
            leg_a_quantity=1.0,
            leg_b_quantity=0.1,
            opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1.0, entry_price=76, mark_price=77))
    db.add(Position(platform="mt5", symbol="USOIL", side="short", quantity=0.1, entry_price=76, mark_price=77))
    db.commit()

    changed = reconcile_orphan_positions(db)

    assert changed == 0
    assert db.query(Alert).filter(Alert.title == "外部孤儿仓位").count() == 0

def test_reconcile_closing_group_advances_to_closed(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("closing")

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            quantity = 1.0 if platform == "hyperliquid" else 0.1
            return {"status": "filled", "external_order_id": external_order_id, "filled_quantity": quantity, "average_price": 100.0, "fee": 0.1}

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "closed"
    assert group.closed_at is not None
    assert group.unrealized_pnl == 0.0
    assert db.query(Fill).count() == 2

def test_reconcile_unreconstructable_pending_order_escalates_manual(monkeypatch) -> None:
    db, group = _pending_reconcile_test_db("opening")
    old_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
    for order in db.query(Order).filter(Order.hedge_group_id == group.id).all():
        order.created_at = old_time
    db.commit()

    class FakeGateway:
        def query_order(self, platform, external_order_id):
            return {"status": "not_ready", "external_order_id": external_order_id, "message": "本地 cache 不包含该订单"}

        def cancel_order(self, platform, external_order_id):
            return True

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())
    monkeypatch.setattr("app.execution.reconciler.get_settings", lambda: SimpleNamespace(execution_reconcile_pending_stale_seconds=1))

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    assert changed
    assert group.status == "manual_intervention"
    assert "不可重建" in group.close_reason
    assert db.query(Alert).filter(Alert.title == "外部订单状态不可重建").count() == 1
    assert db.query(Order).filter(Order.hedge_group_id == group.id, Order.status == "canceled").count() == 2

def test_sync_live_positions_replaces_current_rows(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="mt5", symbol="OLD", side="long", quantity=1, entry_price=1, mark_price=1))
    db.add(SymbolMapping(symbol="JP225", leg_a_venue_symbol="xyz:JP225", mt5_symbol="JP225"))
    db.commit()
    captured = {}

    class FakeHyperAdapter:
        platform = "hyperliquid"

        def __init__(self, live=False):
            pass

        def get_positions(self, dexes=None):
            captured["dexes"] = dexes
            return [{"platform": "hyperliquid", "symbol": "xyz:JP225", "side": "long", "quantity": 0.0002, "entry_price": 71875, "mark_price": 72015, "unrealized_pnl": 0.03}]

    class FakeMT5Adapter:
        platform = "mt5"

        def __init__(self, live=False):
            pass

        def get_positions(self):
            return [{"platform": "mt5", "symbol": "USOIL", "side": "short", "quantity": 0.1, "entry_price": 80, "mark_price": 79, "unrealized_pnl": 1.2}]

    monkeypatch.setattr("app.execution.reconciler.HyperliquidAdapter", FakeHyperAdapter)
    monkeypatch.setattr("app.execution.reconciler.MT5Adapter", FakeMT5Adapter)
    count = sync_live_positions(db)
    db.commit()

    rows = db.query(Position).all()
    assert captured["dexes"] == ["xyz"]
    assert count == 2
    assert [(row.platform, row.symbol, row.side, row.quantity) for row in rows] == [
        ("hyperliquid", "xyz:JP225", "long", 0.0002),
        ("mt5", "USOIL", "short", 0.1),
    ]

def test_reconcile_hyper_maker_fill_submits_mt5_taker(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(SymbolMapping(symbol="EUR", leg_a_venue_symbol="xyz:EUR", mt5_symbol="EURUSD", execution_style="hyper_maker_mt5_taker"))
    group = HedgeGroup(
        symbol="EUR",
        direction="long_leg_b_short_leg_a",
        status="opening",
        execution_mode="paper",
        notional=1145.0,
        quantity=0.01,
        leg_a_quantity=1000.0,
        leg_b_quantity=0.01,
        open_cost=0.2,
    )
    db.add(group)
    db.flush()
    hl_order = Order(
        hedge_group_id=group.id,
        platform="hyperliquid",
        symbol="EUR",
        side="sell",
        quantity=1000.0,
        order_type="limit",
        status="filled",
        external_order_id="hl-maker-1",
    )
    db.add(hl_order)
    db.flush()
    db.add(Fill(order_id=hl_order.id, platform="hyperliquid", symbol="EUR", side="sell", quantity=500.0, price=1.146, fee=0.1))
    db.commit()
    db.refresh(group)
    submitted = []

    class FakeGateway:
        def submit_order(self, intent, *, paper_latency_ms=0):
            submitted.append(intent)
            result = AdapterOrderResult(True, "mt5-taker-1", "filled", intent.quantity, 1.1458, 0.01)
            event = OrderEvent(intent.platform, intent.symbol, intent.side, "filled", result.external_order_id, intent.quantity, intent.quantity, 1.1458, 0.01)
            fill = FillEvent(intent.platform, intent.symbol, intent.side, intent.quantity, 1.1458, 0.01, result.external_order_id)
            return GatewayOrderResult(True, event, (fill,), result)

    monkeypatch.setattr("app.execution.reconciler.build_execution_gateway", lambda adapter: FakeGateway())

    changed = reconcile_hedge_group(db, group)
    db.commit()
    db.refresh(group)

    mt5_order = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5").one()
    assert changed
    assert group.status == "open"
    assert mt5_order.quantity == pytest.approx(0.005)
    assert submitted[0].venue_symbol == "EURUSD"
    assert submitted[0].side == "buy"
    assert db.query(Fill).count() == 2

def test_reconcile_orphan_positions_alerts_unmanaged_live_position() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(Position(platform="hyperliquid", symbol="OIL", side="long", quantity=1.5, entry_price=76, mark_price=77))
    db.commit()

    changed = reconcile_orphan_positions(db)
    changed_again = reconcile_orphan_positions(db)
    db.commit()

    assert changed == 1
    assert changed_again == 0
    alert = db.query(Alert).filter(Alert.title == "外部孤儿仓位").one()
    assert "hyperliquid:OIL:long:1.5" in alert.message
