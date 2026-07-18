"""MT5 Redis 代理协议测试。"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app.core.redis_client import redis_client, redis_key
from app.venues.domain.models import OrderRequest, Side
from app.venues.mt5 import codec
from app.venues.mt5.redis_connector import MT5RedisConnector
from app.venues.domain.models import OrderSnapshot, OrderStatus, OrderType
from mt5_gateway.main import MT5Gateway


def test_mt5_connector_reads_account_snapshot_from_redis() -> None:
    client = redis_client()
    client.set(redis_key("mt5", "snapshot", "account"), codec.dumps({
        "venue": "mt5", "account_id": "10001", "currency": "USD",
        "equity": "10000", "available_balance": "9000", "margin_used": "1000",
        "maintenance_margin": "100", "balances": [], "raw": {"server": "Demo"},
    }))
    account = MT5RedisConnector(redis=client).get_account()
    assert account.account_id == "10001"
    assert account.equity == Decimal("10000")


def test_mt5_rpc_fails_fast_without_gateway_heartbeat() -> None:
    connector = MT5RedisConnector(redis=redis_client())
    with pytest.raises(RuntimeError, match="心跳缺失"):
        connector.get_open_orders()
    assert redis_client().xlen(redis_key("mt5", "commands")) == 0


def test_mt5_submit_order_uses_stream_response_and_idempotency_key() -> None:
    client = redis_client()
    client.set(redis_key("mt5", "health"), codec.dumps({"status": "ok", "connected": True}))
    connector = MT5RedisConnector(read_only=False, redis=client)
    captured: dict[str, str] = {}

    def gateway_once() -> None:
        rows = client.xread({redis_key("mt5", "commands"): "0-0"}, count=1, block=1000)
        _, messages = rows[0]
        _, fields = messages[0]
        captured.update(fields)
        client.xadd(fields["response_stream"], {"ok": "1", "data": codec.dumps({
            "venue": "mt5", "symbol": "EURUSD", "client_order_id": "order-1",
            "venue_order_id": "123", "status": "ACCEPTED", "side": "buy",
            "order_type": "market", "requested_quantity": "0.1",
            "filled_quantity": "0", "remaining_quantity": "0.1",
        }), "error": ""})

    thread = threading.Thread(target=gateway_once)
    thread.start()
    result = connector.submit_order(OrderRequest(
        venue="mt5", symbol="EURUSD", side=Side.BUY,
        quantity=Decimal("0.1"), client_order_id="order-1",
    ))
    thread.join(timeout=2)

    assert result.venue_order_id == "123"
    assert captured["operation"] == "submit_order"
    assert captured["idempotency_key"] == "order-1"


def test_gateway_reuses_idempotent_submit_result() -> None:
    client = redis_client()

    class FakeConnector:
        environment = "demo"

        def __init__(self) -> None:
            self.calls = 0

        def submit_order(self, request):
            self.calls += 1
            return OrderSnapshot(
                venue="mt5", symbol=request.symbol, client_order_id=request.client_order_id,
                venue_order_id="456", status=OrderStatus.ACCEPTED, side=request.side,
                order_type=OrderType.MARKET, requested_quantity=request.quantity,
            )

    native = FakeConnector()
    gateway = MT5Gateway(redis_client=client, connector=native)
    client.xgroup_create(gateway.commands, gateway.group, id="0-0", mkstream=True)
    payload = codec.dumps({"request": codec.primitive(OrderRequest(
        venue="mt5", symbol="EURUSD", side=Side.BUY,
        quantity=Decimal("0.1"), client_order_id="stable-order",
    ))})

    for index in range(2):
        response = redis_key("mt5", "response", f"idem-{index}")
        message_id = client.xadd(gateway.commands, {
            "request_id": f"request-{index}", "operation": "submit_order",
            "payload": payload, "response_stream": response,
            "idempotency_key": "stable-order",
        })
        rows = client.xreadgroup(gateway.group, gateway.consumer, {gateway.commands: ">"}, count=1)
        gateway._handle(message_id, rows[0][1][0][1])
        result = client.xrange(response)[0][1]
        assert result["ok"] == "1"

    assert native.calls == 1
