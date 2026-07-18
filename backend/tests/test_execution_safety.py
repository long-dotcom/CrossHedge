"""交易重构阶段 0 的安全资格与 Paper Probe 冻结测试。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Fill, HedgeGroup, Order
from app.execution.actions import hedge_group_actions, require_group_action


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _group(db, *, status: str, execution_mode: str = "paper") -> HedgeGroup:
    group = HedgeGroup(
        symbol="GOLD",
        direction="long_leg_a_short_leg_b",
        status=status,
        execution_mode=execution_mode,
        notional=4000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.01,
    )
    db.add(group)
    db.commit()
    return group


def test_manual_intervention_without_exposure_cannot_close(db) -> None:
    group = _group(db, status="manual_intervention")

    actions = hedge_group_actions(db, group)

    assert actions["exposure_state"] == "EXPOSURE_UNKNOWN"
    assert actions["close"]["allowed"] is False
    with pytest.raises(ValueError, match="恢复计划"):
        require_group_action(db, group, "close")


def test_paper_open_group_can_close_its_simulated_ledger(db) -> None:
    group = _group(db, status="open", execution_mode="paper")

    actions = hedge_group_actions(db, group)

    assert actions["close"]["allowed"] is True


def test_failed_group_with_fill_exposure_requires_recovery_not_close(db) -> None:
    group = _group(db, status="failed", execution_mode="paper")
    order = Order(
        hedge_group_id=group.id, platform="binance", symbol="GOLD", side="buy",
        quantity=0.001, status="filled", external_order_id="late-fill", reduce_only=False,
    )
    db.add(order)
    db.flush()
    db.add(Fill(order_id=order.id, platform="binance", symbol="GOLD", side="buy", quantity=0.001, price=4000, fee=0.0))
    db.commit()

    actions = hedge_group_actions(db, group)

    assert actions["close"]["allowed"] is False
    assert actions["recover"]["allowed"] is True


def test_live_open_group_requires_attributed_fill_exposure(db) -> None:
    group = _group(db, status="open", execution_mode="live")
    assert hedge_group_actions(db, group)["close"]["allowed"] is False

    order = Order(
        hedge_group_id=group.id,
        platform="binance",
        symbol="GOLD",
        side="buy",
        quantity=0.001,
        status="filled",
        external_order_id="live-open-1",
        reduce_only=False,
    )
    db.add(order)
    db.flush()
    db.add(Fill(order_id=order.id, platform="binance", symbol="GOLD", side="buy", quantity=0.001, price=4000, fee=0.0016))
    db.commit()

    actions = hedge_group_actions(db, group)
    assert actions["exposure_state"] == "EXPOSURE_CONFIRMED"
    assert actions["close"]["allowed"] is True

