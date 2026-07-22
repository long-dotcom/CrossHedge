"""独立 MT5 Gateway 入口。

本程序必须在安装了 MT5 Terminal 的 Windows 主机运行。它是仓库中唯一启动
MetaTrader5 Python API 的运行进程，业务后端通过 Redis Stream 与其通信。
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import traceback
from decimal import Decimal
from typing import Any

from redis import Redis
from redis.exceptions import RedisError, ResponseError
from loguru import logger

# 原生实现保留在独立进程加载路径；业务后端导出的是 Redis 代理连接器。
from app.config.settings import get_settings
from app.core.redis_client import redis_key
from app.venues.mt5 import codec
from mt5_gateway.native_connector import MT5Connector as NativeMT5Connector
from app.venues.domain.models import Side


class MT5Gateway:
    """消费 MT5 命令并维护 Redis 中的只读状态快照。"""

    def __init__(self, *, redis_client=None, connector=None) -> None:
        settings = get_settings()
        if settings.mt5.demo_order_enabled and settings.mt5.live_order_enabled:
            raise RuntimeError("MT5_DEMO_ORDER_ENABLED 与 MT5_LIVE_ORDER_ENABLED 不能同时开启")
        gateway_environment = "demo" if settings.mt5.demo_order_enabled else "live"
        self.settings = settings
        self.redis = redis_client or Redis.from_url(settings.redis.url, decode_responses=True)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.group = "mt5-gateway"
        self.commands = redis_key("mt5", "commands")
        self.events = redis_key("mt5", "events")
        self.stop_event = threading.Event()
        self.symbols: set[str] = set()
        self._instrument_cache: dict[str, Any] = {}
        self._instrument_refresh_at: dict[str, float] = {}
        self._redis_state_lock = threading.Lock()
        self._redis_unavailable = False
        self._last_redis_warning_at = 0.0
        self._redis_retry_initial_seconds = 1.0
        self._redis_retry_max_seconds = 15.0
        self.connector = connector or NativeMT5Connector(
            credentials={"login": settings.mt5.login, "password": settings.mt5.password, "server": settings.mt5.server},
            environment=gateway_environment,
            read_only=not bool(settings.mt5.live_order_enabled or settings.mt5.demo_order_enabled),
            order_deviation_points=settings.mt5.order_deviation_points,
            order_magic=settings.mt5.order_magic,
            poll_interval_ms=settings.mt5.order_poll_interval_ms,
        )
        logger.info(
            "MT5 Gateway 配置: environment={}, demo_order_enabled={}, live_order_enabled={}, read_only={}",
            self.connector.environment,
            settings.mt5.demo_order_enabled,
            settings.mt5.live_order_enabled,
            bool(getattr(self.connector, "read_only", False)),
        )

    def run(self) -> None:
        self.connector.subscribe_private_events(self._publish_event)
        self.connector.start()
        snapshot_thread = threading.Thread(target=self._snapshot_loop, name="mt5-snapshots", daemon=True)
        snapshot_thread.start()
        retry_seconds = self._redis_retry_initial_seconds
        try:
            while not self.stop_event.is_set():
                try:
                    self._run_redis_session()
                    retry_seconds = self._redis_retry_initial_seconds
                except (RedisError, OSError) as exc:
                    if self.stop_event.is_set():
                        break
                    self._mark_redis_unavailable("命令消费", exc)
                    if self.stop_event.wait(retry_seconds):
                        break
                    retry_seconds = min(retry_seconds * 2, self._redis_retry_max_seconds)
        finally:
            self.stop_event.set()
            snapshot_thread.join(timeout=3)
            self.connector.stop()
            self._write_health(False, "Gateway 已停止")

    def _run_redis_session(self) -> None:
        """建立一次 Redis 会话；断线异常交给外层退避重连。"""
        self.redis.ping()
        try:
            self.redis.xgroup_create(self.commands, self.group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._mark_redis_connected()
        self._recover_pending()
        while not self.stop_event.is_set():
            rows = self.redis.xreadgroup(
                self.group, self.consumer, {self.commands: ">"}, count=20, block=1000,
            )
            for _, messages in rows:
                for message_id, fields in messages:
                    self._handle(message_id, fields)

    def stop(self, *_: Any) -> None:
        self.stop_event.set()

    def _handle(self, message_id: str, fields: dict[str, str]) -> None:
        request_id = fields.get("request_id", "")
        response_stream = fields.get("response_stream", "")
        operation = fields.get("operation", "")
        payload = codec.loads(fields.get("payload")) or {}
        idempotency_key = fields.get("idempotency_key", "")
        result_key = redis_key("mt5", "idempotency", operation, idempotency_key) if idempotency_key else ""
        state_key = redis_key("mt5", "idempotency-state", operation, idempotency_key) if idempotency_key else ""
        try:
            cached = self.redis.get(result_key) if result_key else None
            if state_key and not cached and not self.redis.set(state_key, request_id, nx=True):
                raise RuntimeError("同一幂等命令存在结果未知的历史执行，禁止自动重放，请先对账")
            data = codec.loads(cached) if cached else self._dispatch(
                operation,
                payload,
                requested_environment=str(fields.get("environment") or ""),
            )
            serialized = codec.dumps(data)
            if result_key and not cached:
                self.redis.set(result_key, serialized, ex=86400)
                self.redis.delete(state_key)
            self._respond(response_stream, request_id, True, serialized, "")
            self.redis.xack(self.commands, self.group, message_id)
        except Exception as exc:
            self._respond(response_stream, request_id, False, "null", f"{type(exc).__name__}: {exc}")
            # 已产生明确失败响应的命令可以 ACK；调用方决定是否创建新命令。
            self.redis.xack(self.commands, self.group, message_id)

    def _dispatch(self, operation: str, payload: dict[str, Any], *, requested_environment: str = "") -> Any:
        if operation == "get_account":
            return self.connector.get_account()
        if operation == "get_positions":
            return self.connector.get_positions()
        if operation == "get_open_orders":
            return self.connector.get_open_orders(payload.get("symbol"))
        if operation == "get_instruments":
            return self.connector.get_instruments(payload.get("symbols"))
        if operation == "get_instrument":
            return self._instrument_snapshot(payload["symbol"], refresh=bool(payload.get("refresh")))
        if operation == "get_ticker":
            return self.connector.get_ticker(payload["symbol"])
        if operation == "get_order_book":
            return self.connector.get_order_book(payload["symbol"], int(payload.get("depth", 20)))
        if operation == "subscribe_market_data":
            symbols = {str(item) for item in payload.get("symbols", []) if item}
            self.symbols.update(symbols)
            self.connector.subscribe_market_data(sorted(symbols))
            return {"subscribed": sorted(symbols)}
        if operation == "unsubscribe_market_data":
            symbols = {str(item) for item in payload.get("symbols", []) if item}
            self.symbols.difference_update(symbols)
            self.connector.unsubscribe_market_data(sorted(symbols))
            return {"unsubscribed": sorted(symbols)}
        if operation == "submit_order":
            self._require_order_environment(requested_environment)
            return self.connector.submit_order(codec.order_request(payload["request"]))
        if operation == "cancel_order":
            return self.connector.cancel_order(payload["symbol"], client_order_id=payload.get("client_order_id", ""), venue_order_id=payload.get("venue_order_id", ""))
        if operation == "get_order":
            return self.connector.get_order(payload["symbol"], client_order_id=payload.get("client_order_id", ""), venue_order_id=payload.get("venue_order_id", ""))
        if operation == "get_fills":
            return self.connector.get_fills(payload.get("symbol"), client_order_id=payload.get("client_order_id", ""), venue_order_id=payload.get("venue_order_id", ""))
        if operation == "validate_credentials":
            return self.connector.validate_credentials()
        if operation == "order_check":
            return self._order_check(payload)
        raise ValueError(f"未知 MT5 Gateway 操作: {operation}")

    def _require_order_environment(self, requested_environment: str) -> None:
        """每次下单都校验 Gateway 账户环境，Paper 请求只能进入 Demo 账户。"""
        requested = str(requested_environment or "").strip().lower()
        if requested not in {"demo", "live"}:
            return
        account = self.connector.get_account()
        trade_mode = int(account.raw.get("trade_mode", -1))
        if requested == "demo" and trade_mode != 0:
            raise PermissionError(f"Paper 请求要求 MT5 Demo 账户，当前账户为 {account.account_id}")
        if requested == "live" and trade_mode == 0:
            raise PermissionError(f"Live 请求禁止发送到 MT5 Demo 账户: {account.account_id}")

    def _order_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        """在 Gateway 内执行 MT5 order_check，不产生真实订单。"""
        mt5 = self.connector.mt5
        self.connector._ensure_connected_or_raise()
        if payload.get("demo"):
            account = mt5.account_info()
            if account is None or int(getattr(account, "trade_mode", -1)) != 0:
                return {"allowed": False, "message": "MT5 Gateway 当前不是 Demo 账户", "retcode": None, "request": None}
        symbol = str(payload["symbol"])
        side = str(payload["side"]).lower()
        quantity = float(payload["quantity"])
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"allowed": False, "message": f"MT5 tick 不可用: {symbol}", "retcode": None, "request": None}
        is_buy = side == "buy"
        request = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": quantity,
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": float(getattr(tick, "ask" if is_buy else "bid")),
            "deviation": self.settings.mt5.order_deviation_points,
            "magic": self.settings.mt5.order_magic, "type_time": mt5.ORDER_TIME_GTC,
        }
        if payload.get("reduce_only"):
            position = self.connector._matching_position(symbol, Side(side), Decimal(str(quantity)))
            if position is None:
                return {"allowed": False, "message": "MT5 reduce-only 未找到匹配持仓", "retcode": None, "request": request}
            request["position"] = int(getattr(position, "ticket"))
        info = mt5.symbol_info(symbol)
        modes = [int(getattr(info, "filling_mode", getattr(mt5, "ORDER_FILLING_IOC", 1)))]
        for name in ("ORDER_FILLING_IOC", "ORDER_FILLING_FOK", "ORDER_FILLING_RETURN"):
            value = int(getattr(mt5, name, -1))
            if value >= 0 and value not in modes:
                modes.append(value)
        last = None
        for mode in modes:
            candidate = {**request, "type_filling": mode}
            last = mt5.order_check(candidate)
            if last is not None and int(getattr(last, "retcode", -1)) == 0:
                return {"allowed": True, "message": str(getattr(last, "comment", "order_check 通过")), "retcode": 0, "request": candidate}
        return {"allowed": False, "message": str(getattr(last, "comment", mt5.last_error())), "retcode": getattr(last, "retcode", None), "request": request}

    def _respond(self, stream: str, request_id: str, ok: bool, data: str, error: str) -> None:
        if not stream:
            return
        self.redis.xadd(stream, {"request_id": request_id, "ok": "1" if ok else "0", "data": data, "error": error}, maxlen=2)
        self.redis.expire(stream, 60)

    def _recover_pending(self) -> None:
        """接管崩溃消费者遗留的命令，交易命令仍受幂等状态保护。"""
        cursor = "0-0"
        while not self.stop_event.is_set():
            result = self.redis.xautoclaim(
                self.commands, self.group, self.consumer,
                min_idle_time=30000, start_id=cursor, count=20,
            )
            cursor, messages = result[0], result[1]
            for message_id, fields in messages:
                self._handle(message_id, fields)
            if cursor == "0-0" or not messages:
                break

    def _snapshot_loop(self) -> None:
        interval = max(self.settings.quote.mt5_quote_poll_interval_ms / 1000, 0.05)
        while not self.stop_event.is_set():
            try:
                self._snapshot_once()
            except Exception as exc:
                self._write_health(False, f"{type(exc).__name__}: {exc}")
            self.stop_event.wait(interval)

    def _snapshot_once(self) -> None:
        """生成一轮只读快照；品种规格在 Gateway 内低频刷新。"""
        ttl = self.settings.redis.mt5_snapshot_ttl_seconds
        account = self.connector.get_account()
        positions = self.connector.get_positions()
        pipe = self.redis.pipeline(transaction=False)
        pipe.set(redis_key("mt5", "snapshot", "account"), codec.dumps(account), ex=ttl)
        pipe.set(redis_key("mt5", "snapshot", "positions"), codec.dumps(positions), ex=ttl)
        for symbol in tuple(self.symbols):
            instrument = self._instrument_snapshot(symbol)
            ticker = self.connector.get_ticker(symbol)
            pipe.set(redis_key("mt5", "snapshot", "instrument", symbol), codec.dumps(instrument), ex=ttl)
            pipe.set(redis_key("mt5", "ticker", symbol), codec.dumps(ticker), ex=ttl)
        pipe.set(
            redis_key("mt5", "health"), codec.dumps(self._health_payload(True)),
            ex=self.settings.redis.mt5_heartbeat_ttl_seconds,
        )
        pipe.execute()
        self._mark_redis_connected()

    def _instrument_snapshot(self, symbol: str, *, refresh: bool = False):
        normalized = str(symbol)
        now = time.monotonic()
        cached = self._instrument_cache.get(normalized)
        if not refresh and cached is not None and now < self._instrument_refresh_at.get(normalized, 0.0):
            return cached
        instrument = self.connector.get_instrument(normalized, refresh=refresh)
        self._instrument_cache[normalized] = instrument
        interval = max(float(self.settings.venues.instrument_refresh_seconds), 1.0)
        self._instrument_refresh_at[normalized] = now + interval
        return instrument

    def _write_health(self, connected: bool, error: str = "") -> None:
        try:
            self.redis.set(
                redis_key("mt5", "health"), codec.dumps(self._health_payload(connected, error)),
                ex=self.settings.redis.mt5_heartbeat_ttl_seconds,
            )
            self._mark_redis_connected()
        except (RedisError, OSError) as exc:
            self._mark_redis_unavailable("健康状态写入", exc)

    def _health_payload(self, connected: bool, error: str = "") -> dict[str, Any]:
        return {
            "status": "ok" if connected else "degraded", "connected": connected,
            "consumer": self.consumer, "environment": self.connector.environment,
            "read_only": self.connector.read_only,
            "updated_at": time.time(), "error": error,
        }

    def _publish_event(self, event) -> None:
        try:
            self.redis.xadd(self.events, {"data": codec.dumps(event)}, maxlen=10000, approximate=True)
            self._mark_redis_connected()
        except (RedisError, OSError) as exc:
            # Redis 恢复后由订单/持仓对账补齐断线期间事件，不能让回调终止 Gateway。
            self._mark_redis_unavailable("私有事件发布", exc)

    def _mark_redis_unavailable(self, operation: str, exc: Exception) -> None:
        now = time.monotonic()
        with self._redis_state_lock:
            self._redis_unavailable = True
            should_log = now - self._last_redis_warning_at >= 5.0
            if should_log:
                self._last_redis_warning_at = now
        if should_log:
            logger.warning("Redis 暂时不可用，Gateway 将继续重连: operation={}, error={}", operation, exc)

    def _mark_redis_connected(self) -> None:
        with self._redis_state_lock:
            recovered = self._redis_unavailable
            self._redis_unavailable = False
            self._last_redis_warning_at = 0.0
        if recovered:
            logger.info("Redis 连接已恢复，Gateway 继续运行")


def main() -> None:
    gateway = MT5Gateway()
    signal.signal(signal.SIGINT, gateway.stop)
    signal.signal(signal.SIGTERM, gateway.stop)
    try:
        gateway.run()
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
