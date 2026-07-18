"""通过 Redis Stream 调用独立 MT5 Gateway 的 Connector。"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Sequence
from typing import Any

from app.config.settings import get_settings
from app.core.redis_client import redis_client, redis_key
from app.venues.domain.capabilities import VenueCapabilities
from app.venues.domain.models import (
    AccountSnapshot, CredentialCheck, Fill, Instrument, OrderBookSnapshot,
    OrderRequest, OrderSnapshot, Position, Ticker,
)
from app.venues.mt5 import codec
from app.venues.protocols import EventHandler


class MT5RedisConnector:
    """保持 VenueConnector 接口不变，实际操作交给 Gateway。"""

    venue = "mt5"
    capabilities = VenueCapabilities(
        venue="mt5", supports_post_only=False, supports_reduce_only=True,
        supports_hedge_mode=True, supports_client_order_id=False,
        supports_market_websocket=False, supports_private_websocket=False,
        supports_native_order_events=False,
    )

    def __init__(
        self,
        *,
        credentials: dict[str, Any] | None = None,
        environment: str = "live",
        read_only: bool = True,
        redis=None,
        **_: Any,
    ) -> None:
        self.credentials = credentials or {}
        self.environment = str(environment or "live")
        self.read_only = bool(read_only)
        self._redis = redis or redis_client()
        self._handlers: list[EventHandler] = []
        self._stop = threading.Event()
        self._event_thread: threading.Thread | None = None
        self._event_cursor = "$"
        self._subscribed_symbols: set[str] = set()
        self._subscription_lock = threading.Lock()

    def start(self) -> None:
        self._require_gateway()
        if self._event_thread and self._event_thread.is_alive():
            return
        self._stop.clear()
        self._event_thread = threading.Thread(target=self._event_loop, name="mt5-redis-events", daemon=True)
        self._event_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=2)

    def health(self) -> dict:
        raw = self._redis.get(redis_key("mt5", "health"))
        if not raw:
            return {"venue": self.venue, "status": "degraded", "connected": False, "error": "MT5 Gateway 心跳缺失"}
        data = codec.loads(raw)
        return {"venue": self.venue, **data}

    def get_account(self) -> AccountSnapshot:
        raw = self._redis.get(redis_key("mt5", "snapshot", "account"))
        data = codec.loads(raw) if raw else self._rpc("get_account")
        return codec.account(data)

    def get_positions(self) -> list[Position]:
        raw = self._redis.get(redis_key("mt5", "snapshot", "positions"))
        data = codec.loads(raw) if raw else self._rpc("get_positions")
        return [codec.position(item) for item in data]

    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]:
        data = self._rpc("get_open_orders", {"symbol": symbol})
        return [codec.order(item) for item in data]

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]:
        data = self._rpc("get_instruments", {"symbols": list(symbols) if symbols else None})
        return [codec.instrument(item) for item in data]

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        return codec.instrument(self._rpc("get_instrument", {"symbol": symbol, "refresh": refresh}))

    def get_ticker(self, symbol: str) -> Ticker:
        raw = self._redis.get(redis_key("mt5", "ticker", symbol))
        data = codec.loads(raw) if raw else self._rpc("get_ticker", {"symbol": symbol})
        return codec.ticker(data)

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        raw = self._redis.get(redis_key("mt5", "orderbook", symbol))
        data = codec.loads(raw) if raw else self._rpc("get_order_book", {"symbol": symbol, "depth": depth})
        return codec.order_book(data)

    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None:
        if handler:
            self.subscribe_private_events(handler)
        requested = {str(symbol) for symbol in symbols if symbol}
        with self._subscription_lock:
            pending = requested - self._subscribed_symbols
            self._subscribed_symbols.update(pending)
        if pending:
            self._rpc("subscribe_market_data", {"symbols": sorted(pending)})

    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None:
        requested = {str(symbol) for symbol in symbols if symbol}
        with self._subscription_lock:
            pending = requested & self._subscribed_symbols
            self._subscribed_symbols.difference_update(pending)
        if pending:
            self._rpc("unsubscribe_market_data", {"symbols": sorted(pending)})

    def subscribe_private_events(self, handler: EventHandler) -> None:
        if handler not in self._handlers:
            self._handlers.append(handler)

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("MT5 Connector 为只读配置，禁止下单")
        return codec.order(self._rpc("submit_order", {"request": codec.primitive(request)}, idempotency_key=request.client_order_id))

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("MT5 Connector 为只读配置，禁止撤单")
        return codec.order(self._rpc("cancel_order", {
            "symbol": symbol, "client_order_id": client_order_id, "venue_order_id": venue_order_id,
        }, idempotency_key=f"cancel:{venue_order_id}:{client_order_id}"))

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        return codec.order(self._rpc("get_order", {
            "symbol": symbol, "client_order_id": client_order_id, "venue_order_id": venue_order_id,
        }))

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        data = self._rpc("get_fills", {
            "symbol": symbol, "client_order_id": client_order_id, "venue_order_id": venue_order_id,
        })
        return [codec.fill(item) for item in data]

    def validate_credentials(self) -> CredentialCheck:
        return codec.credential_check(self._rpc("validate_credentials"))

    def raw_call(self, operation: str, payload: dict[str, Any] | None = None) -> Any:
        """供尚未领域化的 MT5 读取功能使用，调用仍由 Gateway 执行。"""
        return self._rpc(operation, payload)

    def _rpc(self, operation: str, payload: dict[str, Any] | None = None, *, idempotency_key: str = "") -> Any:
        # 无有效心跳时立即失败，避免向无人消费的 Stream 堆积命令并阻塞到 RPC 超时。
        self._require_gateway()
        request_id = uuid.uuid4().hex
        response_stream = redis_key("mt5", "response", request_id)
        command = {
            "request_id": request_id,
            "operation": operation,
            "payload": codec.dumps(payload or {}),
            "response_stream": response_stream,
            "idempotency_key": idempotency_key,
            "environment": self.environment,
            "created_at": str(time.time()),
            "protocol_version": "1",
        }
        self._redis.xadd(redis_key("mt5", "commands"), command, maxlen=10000, approximate=True)
        timeout_ms = max(int(get_settings().redis.mt5_rpc_timeout_seconds * 1000), 1)
        rows = self._redis.xread({response_stream: "0-0"}, count=1, block=timeout_ms)
        if not rows:
            raise TimeoutError(f"MT5 Gateway 调用超时: {operation}")
        _, messages = rows[0]
        _, fields = messages[0]
        if fields.get("ok") != "1":
            raise RuntimeError(fields.get("error") or f"MT5 Gateway 调用失败: {operation}")
        return codec.loads(fields.get("data"))

    def _require_gateway(self) -> None:
        health = self.health()
        if not health.get("connected"):
            raise RuntimeError(str(health.get("error") or "MT5 Gateway 未连接"))

    def _event_loop(self) -> None:
        stream = redis_key("mt5", "events")
        while not self._stop.is_set():
            try:
                rows = self._redis.xread({stream: self._event_cursor}, count=100, block=1000)
                for _, messages in rows:
                    for message_id, fields in messages:
                        self._event_cursor = message_id
                        value = codec.event(codec.loads(fields["data"]))
                        for handler in tuple(self._handlers):
                            handler(value)
            except Exception:
                if not self._stop.wait(1):
                    continue
