"""公共测试 fixture 和辅助函数，供所有测试文件共享。"""

import os

# 必须在导入 app 模块前启用隔离 Redis，禁止测试误连开发环境实例。
os.environ["ENVIRONMENT"] = "testing"
os.environ["REDIS_URL"] = "redis://test.invalid:6379/15"

from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, HedgeGroup, Order, StrategySetting, SymbolMapping, SystemSetting
from app.market.quotes import quote_cache


import pytest
from app.core.redis_client import redis_client


@pytest.fixture(autouse=True)
def _isolate_redis_cache():
    redis_client().flushall()
    yield
    redis_client().flushall()


def _pending_reconcile_test_db(status: str):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status=status,
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        open_cost=1.0,
        fees=0.2,
        unrealized_pnl=5.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None) if status == "closing" else None,
    )
    db.add(group)
    db.flush()
    db.add_all(
        [
            Order(hedge_group_id=group.id, platform="hyperliquid", symbol="OIL", side="buy" if status == "opening" else "sell", quantity=1.0, status="accepted", external_order_id="hl-1"),
            Order(hedge_group_id=group.id, platform="mt5", symbol="OIL", side="sell" if status == "opening" else "buy", quantity=0.1, status="accepted", external_order_id="12345"),
        ]
    )
    db.commit()
    db.refresh(group)
    return db, group

def _seed_auto_close_quotes() -> None:
    quote_cache.put("hyperliquid", "OIL", bid=100.0, ask=101.0, depth_notional=10000, source="test")
    quote_cache.put("mt5", "OIL", bid=100.5, ask=101.0, depth_notional=10000, source="test")

def _live_close_test_db(auto_close_live_enabled: bool = False, live_trading_enabled: bool = True):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(
        StrategySetting(
            execution_mode="live",
            auto_close_enabled=True,
            auto_close_live_enabled=auto_close_live_enabled,
            auto_close_min_profit=0.0,
            max_holding_minutes=240,
        )
    )
    db.add(SystemSetting(key="live_trading_enabled", value="true" if live_trading_enabled else "false"))
    db.add(
        SymbolMapping(
            symbol="OIL",
            leg_a_venue_symbol="OIL",
            mt5_symbol="USOIL",
            allow_hold_through_mt5_close=True,
            hl_close_order_type="market",
            mt5_close_order_type="market",
        )
    )
    group = HedgeGroup(
        symbol="OIL",
        direction="long_leg_a_short_leg_b",
        status="open",
        execution_mode="live",
        notional=1000,
        quantity=1.0,
        leg_a_quantity=1.0,
        leg_b_quantity=0.1,
        open_cost=1.0,
        entry_spread=10.0,
        exit_target=2.0,
        fees=0.2,
        unrealized_pnl=5.0,
        opened_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(group)
    db.commit()
    return db, group.id
