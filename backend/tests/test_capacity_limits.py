"""品种级与全局开仓容量限制测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import Base, HedgeGroup, RiskSetting, SymbolMapping
from app.risk.engine import open_capacity_check


def _db(*, risk_values=None, mapping_values=None):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    risk_defaults = dict(
        max_order_notional=2000,
        max_total_open_notional=10000,
        max_global_open_groups=10,
        max_pending_open_groups=5,
        max_daily_loss=0,
    )
    risk_defaults.update(risk_values or {})
    mapping_defaults = dict(
        symbol="BTC",
        leg_a_venue_symbol="BTCUSDT",
        mt5_symbol="BTCUSD",
        target_notional=1000,
        max_open_notional=5000,
        max_open_groups=5,
        open_cooldown_seconds=0,
        max_daily_opens=0,
        max_daily_open_notional=0,
        allow_opposite_direction=False,
    )
    mapping_defaults.update(mapping_values or {})
    risk = RiskSetting(**risk_defaults)
    mapping = SymbolMapping(**mapping_defaults)
    db.add_all([risk, mapping])
    db.commit()
    return db


def _group(db, *, symbol="BTC", direction="long_leg_a_short_leg_b", status="open", notional=1000, realized_pnl=0, closed=False):
    row = HedgeGroup(
        symbol=symbol,
        direction=direction,
        status=status,
        execution_mode="paper",
        notional=notional,
        quantity=1,
        leg_a_quantity=1,
        leg_b_quantity=1,
        realized_pnl=realized_pnl,
        closed_at=utc_now() if closed else None,
    )
    db.add(row)
    db.commit()
    return row


def test_symbol_open_notional_limit_blocks_stacking():
    db = _db(mapping_values={"max_open_notional": 1500})
    _group(db, notional=1000)
    decision = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 600)
    assert not decision.allowed
    assert "品种上限" in decision.reason


def test_opposite_direction_is_blocked_by_default():
    db = _db()
    _group(db, direction="long_leg_a_short_leg_b")
    decision = open_capacity_check(db, "BTC", "long_leg_b_short_leg_a", 500)
    assert not decision.allowed
    assert "禁止双向" in decision.reason


def test_global_pending_and_total_notional_limits():
    db = _db(risk_values={"max_pending_open_groups": 1, "max_total_open_notional": 1500})
    _group(db, status="opening", notional=1000)
    pending = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 400)
    assert not pending.allowed
    assert "在途" in pending.reason

    db.query(RiskSetting).first().max_pending_open_groups = 5
    db.commit()
    total = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 600)
    assert not total.allowed
    assert "全局累计" in total.reason


def test_cooldown_uses_persisted_group_timestamp():
    db = _db(mapping_values={"open_cooldown_seconds": 60})
    _group(db, status="closed", closed=True)
    decision = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 500)
    assert not decision.allowed
    assert "冷却中" in decision.reason


def test_daily_count_notional_and_loss_limits():
    db = _db(
        risk_values={"max_daily_loss": 500},
        mapping_values={"max_daily_opens": 2, "max_daily_open_notional": 1500},
    )
    _group(db, status="closed", notional=800, realized_pnl=-600, closed=True)

    loss = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 500)
    assert not loss.allowed
    assert "已实现亏损" in loss.reason

    db.query(RiskSetting).first().max_daily_loss = 0
    db.commit()
    amount = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 800)
    assert not amount.allowed
    assert "今日累计" in amount.reason

    db.query(SymbolMapping).first().max_daily_open_notional = 0
    db.commit()
    _group(db, status="closed", notional=100, closed=True)
    count = open_capacity_check(db, "BTC", "long_leg_a_short_leg_b", 100)
    assert not count.allowed
    assert "今日开仓次数" in count.reason
