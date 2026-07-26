"""跨实例共享状态必须落在 Redis，而不是进程内字典。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.time_utils import utc_now
from app.db.models import Base, HedgeGroup
from app.execution.hedge_pool import HedgeGroupSnapshot, HedgePoolStore
from app.market.scan_state import ScanStateStore


def _group() -> HedgeGroupSnapshot:
    return HedgeGroupSnapshot(
        id=7, symbol="BTC", direction="long_leg_a_short_leg_b", status="open",
        execution_mode="paper", notional=1000, quantity=1, leg_b_quantity=1,
        leg_a_quantity=1, open_cost=0, fees=0, funding=0, swap=0,
        realized_pnl=0, unrealized_pnl=3, trigger_spread=20, entry_spread=20,
        entry_threshold=20, exit_target=10, overheat_threshold=0, close_reason="",
        opened_at=datetime(2026, 1, 1), closed_at=None, source="test",
    )


def test_hedge_pool_is_shared_and_closing_claim_is_atomic() -> None:
    first = HedgePoolStore(key="test:hedge-pool")
    second = HedgePoolStore(key="test:hedge-pool")
    first.upsert_group(_group())

    assert second.get(7) == _group()
    claimed = second.try_mark_closing(7, reason="target")
    assert claimed is not None and claimed.status == "closing"
    assert first.try_mark_closing(7, reason="duplicate") is None


def test_scan_state_is_shared_between_instances() -> None:
    writer = ScanStateStore(key="test:scan-state")
    reader = ScanStateStore(key="test:scan-state")
    writer.update([{"symbol": "BTC"}], [{"symbol": "BTC", "direction": "long"}])

    assert reader.snapshot()["spreads"] == [{"symbol": "BTC"}]
    reader.merge_opportunity_ids({("BTC", "long"): 42})
    assert writer.snapshot()["opportunities"][0]["id"] == 42


def test_database_reload_does_not_overwrite_newer_concurrent_pool_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, future=True)()
    old_time = utc_now() - timedelta(minutes=1)
    group = HedgeGroup(
        symbol="BTC", direction="long_leg_a_short_leg_b", status="open",
        execution_mode="paper", notional=1000, quantity=1,
        leg_a_quantity=1, leg_b_quantity=1, updated_at=old_time,
    )
    db.add(group)
    db.commit()
    store = HedgePoolStore(key="test:hedge-pool-reload")
    newer = HedgeGroupSnapshot.from_row(group).with_updates(
        status="closing", updated_at=utc_now(), close_reason="concurrent close",
    )
    store.upsert_group(newer)

    store.load_from_db(db)

    persisted = store.get(group.id)
    assert persisted is not None
    assert persisted.status == "closing"
    assert persisted.close_reason == "concurrent close"
