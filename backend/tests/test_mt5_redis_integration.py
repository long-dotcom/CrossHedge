"""使用真实 Redis 验证后端代理与独立 Gateway 的端到端协议。"""

from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest
from redis import Redis

from app.core.redis_client import redis_key
from app.core.time_utils import utc_now
from app.venues.domain.events import VenueEvent, VenueEventType
from app.venues.domain.models import AccountSnapshot, OrderRequest, OrderSnapshot, OrderStatus, OrderType, Side
from app.venues.mt5 import codec
from app.venues.mt5.redis_connector import MT5RedisConnector
from mt5_gateway.main import MT5Gateway


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_REDIS_INTEGRATION") != "1",
    reason="设置 RUN_REDIS_INTEGRATION=1 后运行真实 Redis 集成测试",
)


class _FakeNativeConnector:
    environment = "demo"

    def __init__(self) -> None:
        self.submit_calls = 0
        self.cancel_calls = 0

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        self.submit_calls += 1
        return OrderSnapshot(
            venue="mt5", symbol=request.symbol, client_order_id=request.client_order_id,
            venue_order_id="redis-e2e-1", status=OrderStatus.ACCEPTED, side=request.side,
            order_type=OrderType.MARKET, requested_quantity=request.quantity,
        )

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            venue="mt5", account_id="demo-10001", currency="USD",
            equity=Decimal("10000"), available_balance=Decimal("9000"),
            margin_used=Decimal("1000"), maintenance_margin=Decimal("100"),
        )

    def cancel_order(self, symbol: str, **kwargs) -> OrderSnapshot:
        self.cancel_calls += 1
        return OrderSnapshot(
            venue="mt5", symbol=symbol, client_order_id=kwargs["client_order_id"],
            venue_order_id=kwargs["venue_order_id"], status=OrderStatus.PENDING_CANCEL,
            side=Side.BUY, order_type=OrderType.LIMIT, requested_quantity=Decimal("0.1"),
        )

    def get_positions(self) -> list:
        return []


def test_real_redis_command_snapshot_event_and_idempotency() -> None:
    client = Redis.from_url(
        os.getenv("REDIS_INTEGRATION_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    client.ping()
    client.flushdb()
    native = _FakeNativeConnector()
    gateway = MT5Gateway(redis_client=client, connector=native)
    client.xgroup_create(gateway.commands, gateway.group, id="0-0", mkstream=True)
    client.set(redis_key("mt5", "health"), codec.dumps({"status": "ok", "connected": True}), ex=30)
    proxy = MT5RedisConnector(read_only=False, redis=client)

    def consume(count: int) -> None:
        for _ in range(count):
            rows = client.xreadgroup(gateway.group, gateway.consumer, {gateway.commands: ">"}, count=1, block=2000)
            assert rows
            message_id, fields = rows[0][1][0]
            gateway._handle(message_id, fields)

    consumer = threading.Thread(target=consume, args=(3,))
    consumer.start()
    request = OrderRequest(
        venue="mt5", symbol="EURUSD", side=Side.BUY,
        quantity=Decimal("0.1"), client_order_id="stable-real-redis",
    )
    first = proxy.submit_order(request)
    second = proxy.submit_order(request)
    canceled = proxy.cancel_order(
        "EURUSD", client_order_id=request.client_order_id, venue_order_id=first.venue_order_id,
    )
    consumer.join(timeout=5)

    assert first.venue_order_id == second.venue_order_id == "redis-e2e-1"
    assert native.submit_calls == 1
    assert canceled.status == OrderStatus.PENDING_CANCEL
    assert native.cancel_calls == 1

    ttl = gateway.settings.redis.mt5_snapshot_ttl_seconds
    client.set(redis_key("mt5", "snapshot", "account"), codec.dumps(native.get_account()), ex=ttl)
    client.set(redis_key("mt5", "snapshot", "positions"), codec.dumps(native.get_positions()), ex=ttl)
    assert proxy.get_account().account_id == "demo-10001"
    assert proxy.get_positions() == []

    gateway._publish_event(VenueEvent(
        event_id="redis-event-1", venue="mt5", event_type=VenueEventType.ORDER_ACCEPTED,
        occurred_at=utc_now(),
        order=first,
    ))
    event_fields = client.xrevrange(gateway.events, count=1)[0][1]
    assert codec.event(codec.loads(event_fields["data"])).event_id == "redis-event-1"
    client.flushdb()
