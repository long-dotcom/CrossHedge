"""自动平仓场所路由与执行成本口径回归测试。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.execution.auto_closer import evaluate_auto_close, run_auto_close
from app.execution.hedge_pool import HedgeGroupSnapshot
from app.execution.pnl import liquidation_pnl_from_close_spread, pnl_from_close_spread


def _snapshot(**overrides) -> HedgeGroupSnapshot:
    values = {
        "id": 1, "symbol": "GOLD", "direction": "long_leg_a_short_leg_b", "status": "open",
        "execution_mode": "live", "notional": 1000.0, "quantity": 1.0,
        "leg_b_quantity": 1.0, "leg_a_quantity": 1.0, "open_cost": 0.0,
        "fees": 1.5, "funding": 999.0, "swap": 999.0,
        "realized_pnl": 0.0, "unrealized_pnl": 0.0, "trigger_spread": 3.0,
        "entry_spread": 3.0, "entry_threshold": 2.0, "exit_target": 1.0,
        "overheat_threshold": 0.0, "close_reason": "", "opened_at": None,
        "closed_at": None, "source": "test",
    }
    values.update(overrides)
    return HedgeGroupSnapshot(**values)


def test_auto_close_uses_mapping_venues_for_quote_sync(monkeypatch) -> None:
    calls: list[dict] = []
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=100.0, ask=101.0),
        leg_b=SimpleNamespace(bid=101.0, ask=102.0),
    )

    def synchronized(*_args, **kwargs):
        calls.append(kwargs)
        return synced, ""

    monkeypatch.setattr("app.execution.auto_closer.quote_synchronizer.synchronized", synchronized)
    monkeypatch.setattr("app.execution.auto_closer.estimated_pair_close_fee", lambda *_args: 0.0)
    strategy = SimpleNamespace()
    mapping = SimpleNamespace(
        symbol="GOLD", leg_a_venue="binance", leg_b_venue="mt5",
        max_close_spread=0.0,
    )

    evaluate_auto_close(SimpleNamespace(), strategy, _snapshot(), mapping=mapping)

    assert calls[0]["leg_a_venue"] == "binance"
    assert calls[0]["leg_b_venue"] == "mt5"


def test_auto_close_does_not_block_negative_estimated_profit(monkeypatch) -> None:
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=100.0, ask=101.0),
        leg_b=SimpleNamespace(bid=101.0, ask=102.0),
    )
    monkeypatch.setattr(
        "app.execution.auto_closer.quote_synchronizer.synchronized",
        lambda *_args, **_kwargs: (synced, ""),
    )
    monkeypatch.setattr("app.execution.auto_closer.estimated_pair_close_fee", lambda *_args: 0.0)
    strategy = SimpleNamespace()
    mapping = SimpleNamespace(
        symbol="GOLD", leg_a_venue="binance", leg_b_venue="mt5",
        max_close_spread=0.0,
    )

    evaluation = evaluate_auto_close(
        SimpleNamespace(),
        strategy,
        _snapshot(exit_target=3.0, fees=100.0),
        mapping=mapping,
    )

    assert evaluation.should_close is True
    assert evaluation.estimated_profit < 0


def test_auto_close_does_not_close_only_because_position_is_old(monkeypatch) -> None:
    synced = SimpleNamespace(
        leg_a=SimpleNamespace(bid=100.0, ask=101.0),
        leg_b=SimpleNamespace(bid=101.0, ask=102.0),
    )
    monkeypatch.setattr(
        "app.execution.auto_closer.quote_synchronizer.synchronized",
        lambda *_args, **_kwargs: (synced, ""),
    )
    monkeypatch.setattr("app.execution.auto_closer.estimated_pair_close_fee", lambda *_args: 0.0)
    mapping = SimpleNamespace(
        symbol="GOLD", leg_a_venue="binance", leg_b_venue="mt5", max_close_spread=0.0,
    )

    evaluation = evaluate_auto_close(
        SimpleNamespace(),
        SimpleNamespace(),
        _snapshot(exit_target=1.0, opened_at=datetime.now() - timedelta(days=365)),
        mapping=mapping,
    )

    assert evaluation.should_close is False
    assert evaluation.reason.startswith("等待平仓价差回归")


def test_pnl_ignores_legacy_funding_and_swap_fields() -> None:
    group = _snapshot(entry_spread=10.0, fees=1.5, funding=999.0, swap=999.0)

    assert pnl_from_close_spread(group, 4.0) == 4.5


def test_liquidation_pnl_includes_remaining_close_fee() -> None:
    group = _snapshot(entry_spread=10.0, fees=1.5, estimated_close_fee=0.75)

    assert pnl_from_close_spread(group, 4.0) == 4.5
    assert liquidation_pnl_from_close_spread(group, 4.0) == 3.75


def test_auto_close_pauses_closed_mt5_symbol_without_logging_failure(monkeypatch) -> None:
    """休市品种不应继续评估并触发每秒异常日志。"""
    snapshot = SimpleNamespace(id=138, symbol="XAG")
    group = SimpleNamespace(id=138, symbol="XAG", status="open")
    mapping = SimpleNamespace(symbol="XAG", leg_a_venue="hyperliquid", leg_b_venue="mt5")
    db = SimpleNamespace(get=lambda *_args: group)

    monkeypatch.setattr(
        "app.execution.auto_closer.get_strategy_setting",
        lambda *_args: SimpleNamespace(auto_close_enabled=True, auto_close_live_enabled=True),
    )
    monkeypatch.setattr("app.execution.auto_closer.enabled_mappings", lambda *_args: [mapping])
    monkeypatch.setattr("app.execution.auto_closer.hedge_pool.snapshot_open_groups", lambda *_args: [snapshot])
    monkeypatch.setattr(
        "app.execution.auto_closer.mt5_session_state",
        lambda *_args: SimpleNamespace(symbol_flow_paused=True),
    )
    monkeypatch.setattr(
        "app.execution.auto_closer.evaluate_auto_close",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("休市时不应评估平仓")),
    )

    assert run_auto_close(db) == 0
