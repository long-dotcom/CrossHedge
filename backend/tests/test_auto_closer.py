"""自动平仓场所路由与执行成本口径回归测试。"""

from types import SimpleNamespace

from app.execution.auto_closer import evaluate_auto_close
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
    strategy = SimpleNamespace(auto_close_min_profit=0.0, max_holding_minutes=60)
    mapping = SimpleNamespace(
        symbol="GOLD", leg_a_venue="binance", leg_b_venue="mt5",
        max_close_spread=0.0, max_holding_minutes=60,
    )

    evaluate_auto_close(SimpleNamespace(), strategy, _snapshot(), mapping=mapping)

    assert calls[0]["leg_a_venue"] == "binance"
    assert calls[0]["leg_b_venue"] == "mt5"


def test_pnl_ignores_legacy_funding_and_swap_fields() -> None:
    group = _snapshot(entry_spread=10.0, fees=1.5, funding=999.0, swap=999.0)

    assert pnl_from_close_spread(group, 4.0) == 4.5


def test_liquidation_pnl_includes_remaining_close_fee() -> None:
    group = _snapshot(entry_spread=10.0, fees=1.5, estimated_close_fee=0.75)

    assert pnl_from_close_spread(group, 4.0) == 4.5
    assert liquidation_pnl_from_close_spread(group, 4.0) == 3.75
