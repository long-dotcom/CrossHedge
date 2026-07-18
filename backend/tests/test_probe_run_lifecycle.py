"""独立 ProbeRun 的真实最小量、明确回平和仓位基线测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ExecutionIntent, ExecutionLeg, ProbeRun, SymbolMapping
from app.execution.outbox_worker import run_execution_outbox_once
from app.execution.probe_runs import create_probe_run, probe_run_payload
from tests.native_fakes import order_snapshot


class ProbeAdapter:
    platform = "binance"

    def __init__(self, state: dict) -> None:
        self.state = state

    def probe_quantity(self, symbol: str) -> float:
        return 0.001

    def get_positions(self):
        quantity = float(self.state.get("position", 0.142))
        return [{
            "platform": "binance",
            "symbol": "XAUUSDT-PERP.BINANCE",
            "side": "long",
            "quantity": quantity,
        }] if quantity > 0 else []

    def submit_order(self, order):
        self.state.setdefault("orders", []).append(order)
        if order.side == "buy":
            self.state["position"] = float(self.state.get("position", 0.142)) + float(order.quantity)
            return order_snapshot(order, filled=float(order.quantity), price=4000, commission=0.001, venue_order_id="probe-entry")
        exit_fill = float(order.quantity) * float(self.state.get("exit_fill_ratio", 1.0))
        self.state["position"] = max(float(self.state.get("position", 0.142)) - exit_fill, 0.0)
        status = "filled" if exit_fill >= float(order.quantity) - 1e-12 else "rejected"
        return order_snapshot(order, status=status, filled=exit_fill, price=4000.5, commission=0.001, venue_order_id="probe-exit")

    def get_order(self, symbol, **kwargs):
        return order_snapshot(venue="binance", symbol=symbol, status="unknown")


class ShortProbeAdapter:
    platform = "binance"

    def __init__(self, state: dict) -> None:
        self.state = state

    def probe_quantity(self, symbol: str) -> float:
        return 0.001

    def get_positions(self):
        quantity = float(self.state.get("position", 0.082))
        return [{
            "platform": "binance",
            "symbol": "XAUUSDT-PERP.BINANCE",
            "side": "short",
            "quantity": quantity,
        }] if quantity > 0 else []

    def submit_order(self, order):
        self.state.setdefault("orders", []).append(order)
        if order.side == "sell":
            self.state["position"] = float(self.state.get("position", 0.082)) + float(order.quantity)
            return order_snapshot(order, filled=float(order.quantity), price=4000, commission=0.001, venue_order_id="probe-short-entry")
        self.state["position"] = max(float(self.state.get("position", 0.082)) - float(order.quantity), 0.0)
        return order_snapshot(order, filled=float(order.quantity), price=3999.5, commission=0.001, venue_order_id="probe-short-exit")

    def get_order(self, symbol, **kwargs):
        return order_snapshot(venue="binance", symbol=symbol, status="unknown")


def _factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _mapping(db) -> None:
    db.add(SymbolMapping(
        symbol="GOLD",
        leg_a_venue="binance",
        leg_a_venue_symbol="XAUUSDT",
        leg_a_symbol="XAUUSDT",
        leg_b_venue="mt5",
        leg_b_symbol="XAUUSD",
        mt5_symbol="XAUUSD",
    ))
    db.commit()


def test_probe_uses_actual_minimum_and_returns_to_existing_position_baseline(monkeypatch) -> None:
    factory = _factory()
    state = {"position": 0.142}
    adapter = ProbeAdapter(state)
    monkeypatch.setattr("app.execution.probe_runs.paper_live_probe_enabled_for_venue", lambda *args: True)
    monkeypatch.setattr("app.execution.probe_runs.build_market_adapter", lambda *args, **kwargs: adapter)
    with factory() as db:
        _mapping(db)
        run, result = create_probe_run(
            db, symbol="GOLD", venue="binance", side="buy", purpose="CONNECTIVITY",
            requested_by="test", idempotency_key="probe:gold:1",
        )
        db.commit()
        run_id = run.id
        assert run.probe_quantity == 0.001
        assert run.baseline_position_quantity == 0.142
        assert result.intent.execution_mode == "live"

    adapter_factory = lambda venue, mode: adapter
    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        run = db.get(ProbeRun, run_id)
        assert run.status == "FLATTENING"
        assert run.exit_intent_id is not None
        exit_leg = db.query(ExecutionLeg).filter_by(intent_id=run.exit_intent_id).one()
        assert exit_leg.order_side == "SELL"
        assert exit_leg.position_side == "LONG"
        assert exit_leg.venue_reduce_only is False
        assert exit_leg.venue_order_quantity == 0.001

    assert run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory) == 1
    with factory() as db:
        run = db.get(ProbeRun, run_id)
        assert run.status == "FLAT"
        assert run.residual_quantity == 0.0
        assert run.final_position_quantity == 0.142
        assert run.flat_confirmed_at is not None
        assert db.get(ExecutionIntent, run.entry_intent_id).status == "COMPLETED"
        assert db.get(ExecutionIntent, run.exit_intent_id).status == "COMPLETED"
        payload = probe_run_payload(db, run)
        assert payload["entry"]["status"] == "COMPLETED"
        assert payload["entry"]["order_side"] == "BUY"
        assert payload["entry"]["position_side"] == "LONG"
        assert payload["entry"]["venue_reduce_only"] is False
        assert payload["entry"]["order"]["status"] == "FILLED"
        assert payload["entry"]["order"]["filled_quantity"] == 0.001
        assert payload["entry"]["order"]["remaining_quantity"] == 0.0
        assert payload["exit"]["status"] == "COMPLETED"
        assert payload["exit"]["order_side"] == "SELL"
        assert payload["exit"]["position_side"] == "LONG"
        assert payload["exit"]["venue_reduce_only"] is False
        assert payload["exit"]["order"]["status"] == "FILLED"
        assert payload["exit"]["order"]["venue_order_id"] == "probe-exit"
    assert [(order.side, order.position_side, order.reduce_only) for order in state["orders"]] == [
        ("buy", "LONG", False),
        ("sell", "LONG", False),
    ]


def test_probe_idempotency_reuses_original_run(monkeypatch) -> None:
    factory = _factory()
    adapter = ProbeAdapter({"position": 0.0})
    monkeypatch.setattr("app.execution.probe_runs.paper_live_probe_enabled_for_venue", lambda *args: True)
    monkeypatch.setattr("app.execution.probe_runs.build_market_adapter", lambda *args, **kwargs: adapter)
    with factory() as db:
        _mapping(db)
        first, first_result = create_probe_run(
            db, symbol="GOLD", venue="binance", side="buy", purpose="CONNECTIVITY",
            requested_by="test", idempotency_key="probe:same",
        )
        db.commit()
        second, second_result = create_probe_run(
            db, symbol="GOLD", venue="binance", side="buy", purpose="CONNECTIVITY",
            requested_by="test", idempotency_key="probe:same",
        )
        assert first.id == second.id
        assert first_result.intent.id == second_result.intent.id
        assert second_result.created is False
        assert db.query(ProbeRun).count() == 1


def test_short_probe_opens_and_closes_same_short_position_side(monkeypatch) -> None:
    factory = _factory()
    state = {"position": 0.082}
    adapter = ShortProbeAdapter(state)
    monkeypatch.setattr("app.execution.probe_runs.paper_live_probe_enabled_for_venue", lambda *args: True)
    monkeypatch.setattr("app.execution.probe_runs.build_market_adapter", lambda *args, **kwargs: adapter)
    with factory() as db:
        _mapping(db)
        run, _ = create_probe_run(
            db, symbol="GOLD", venue="binance", side="sell", purpose="CONNECTIVITY",
            requested_by="test", idempotency_key="probe:gold:short",
        )
        db.commit()
        run_id = run.id

    adapter_factory = lambda venue, mode: adapter
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)

    with factory() as db:
        payload = probe_run_payload(db, db.get(ProbeRun, run_id))
        assert payload["status"] == "FLAT"
        assert payload["final_position_quantity"] == 0.082
        assert payload["entry"]["order_side"] == "SELL"
        assert payload["entry"]["position_side"] == "SHORT"
        assert payload["entry"]["venue_reduce_only"] is False
        assert payload["exit"]["order_side"] == "BUY"
        assert payload["exit"]["position_side"] == "SHORT"
        assert payload["exit"]["venue_reduce_only"] is False
    assert [(order.side, order.position_side, order.reduce_only) for order in state["orders"]] == [
        ("sell", "SHORT", False),
        ("buy", "SHORT", False),
    ]


def test_partial_probe_exit_enters_recovery_and_never_reports_flat(monkeypatch) -> None:
    factory = _factory()
    state = {"position": 0.142, "exit_fill_ratio": 0.5}
    adapter = ProbeAdapter(state)
    monkeypatch.setattr("app.execution.probe_runs.paper_live_probe_enabled_for_venue", lambda *args: True)
    monkeypatch.setattr("app.execution.probe_runs.build_market_adapter", lambda *args, **kwargs: adapter)
    with factory() as db:
        _mapping(db)
        run, _ = create_probe_run(
            db, symbol="GOLD", venue="binance", side="buy", purpose="CONNECTIVITY",
            requested_by="test", idempotency_key="probe:partial-exit",
        )
        db.commit()
        run_id = run.id
    adapter_factory = lambda venue, mode: adapter
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    run_execution_outbox_once(session_factory=factory, adapter_factory=adapter_factory)
    with factory() as db:
        run = db.get(ProbeRun, run_id)
        assert run.status == "RECOVERY_REQUIRED"
        assert run.residual_quantity == 0.0005
        assert run.flat_confirmed_at is None
        assert db.get(ExecutionIntent, run.exit_intent_id).status == "RECOVERY_REQUIRED"
