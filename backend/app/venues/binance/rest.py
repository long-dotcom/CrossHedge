"""Binance USDⓈ-M Futures 原生 REST 客户端。

只依赖 Python 标准库，签名、时间同步和错误分类均由项目自身维护。
交易请求出现 HTTP 5xx 或超时时会标记为结果未知，调用方必须通过
clientOrderId 或用户数据流确认，禁止直接重发。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any
from urllib import error, parse, request


BINANCE_FUTURES_URLS = {
    "live": "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "demo": "https://demo-fapi.binance.com",
}


@dataclass(frozen=True)
class BinanceResponse:
    data: Any
    status: int
    headers: dict[str, str]


class BinanceApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
        self.outcome_unknown = outcome_unknown


class BinanceFuturesRestClient:
    """同步原生 REST 客户端，适合执行 Worker 和定时同步任务。"""

    def __init__(
        self,
        *,
        api_key: str = "",
        api_secret: str = "",
        environment: str = "live",
        timeout: float = 10.0,
        recv_window_ms: int = 10_000,
        transport=None,
    ) -> None:
        normalized_environment = str(environment or "live").strip().lower()
        if normalized_environment == "sandbox":
            normalized_environment = "live"
        if normalized_environment not in BINANCE_FUTURES_URLS:
            raise ValueError(f"不支持的 Binance 环境: {environment}")
        self.api_key = str(api_key or "")
        self.api_secret = str(api_secret or "")
        self.environment = normalized_environment
        self.base_url = BINANCE_FUTURES_URLS[normalized_environment]
        self.timeout = max(float(timeout), 0.1)
        self.recv_window_ms = max(int(recv_window_ms), 1_000)
        self._transport = transport or self._urlopen_transport
        self._clock_offset_ms = 0
        self._lock = RLock()

    def server_time(self) -> int:
        payload = self.public("GET", "/fapi/v1/time")
        return int(payload["serverTime"])

    def synchronize_clock(self) -> int:
        started = self._local_time_ms()
        server = self.server_time()
        ended = self._local_time_ms()
        midpoint = (started + ended) // 2
        with self._lock:
            self._clock_offset_ms = server - midpoint
        return self._clock_offset_ms

    def exchange_info(self) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/exchangeInfo")

    def depth(self, symbol: str, limit: int = 1000) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/depth", {"symbol": normalize_symbol(symbol), "limit": limit})

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/ticker/bookTicker", {"symbol": normalize_symbol(symbol)})

    def premium_index(self, symbol: str) -> dict[str, Any]:
        return self.public("GET", "/fapi/v1/premiumIndex", {"symbol": normalize_symbol(symbol)})

    def funding_info(self) -> list[dict[str, Any]]:
        payload = self.public("GET", "/fapi/v1/fundingInfo")
        return payload if isinstance(payload, list) else []

    def funding_history(self, symbol: str, start_ms: int, end_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        payload = self.public(
            "GET",
            "/fapi/v1/fundingRate",
            {"symbol": normalize_symbol(symbol), "startTime": start_ms, "endTime": end_ms, "limit": limit},
        )
        return payload if isinstance(payload, list) else []

    def account(self) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v3/account")

    def position_risk(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": normalize_symbol(symbol)} if symbol else {}
        payload = self.signed("GET", "/fapi/v3/positionRisk", params)
        return payload if isinstance(payload, list) else []

    def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"symbol": normalize_symbol(symbol)} if symbol else {}
        payload = self.signed("GET", "/fapi/v1/openOrders", params)
        return payload if isinstance(payload, list) else []

    def commission_rate(self, symbol: str) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v1/commissionRate", {"symbol": normalize_symbol(symbol)})

    def position_mode(self) -> dict[str, Any]:
        return self.signed("GET", "/fapi/v1/positionSide/dual")

    def query_order(self, symbol: str, *, order_id: str = "", client_order_id: str = "") -> dict[str, Any]:
        params = self._order_identity(symbol, order_id=order_id, client_order_id=client_order_id)
        return self.signed("GET", "/fapi/v1/order", params)

    def user_trades(
        self,
        symbol: str,
        *,
        order_id: str = "",
        start_time: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": normalize_symbol(symbol), "limit": limit}
        if order_id:
            params["orderId"] = order_id
        if start_time is not None:
            params["startTime"] = int(start_time)
        payload = self.signed("GET", "/fapi/v1/userTrades", params)
        return payload if isinstance(payload, list) else []

    def place_order(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.signed("POST", "/fapi/v1/order", params, order_operation=True)

    def cancel_order(self, symbol: str, *, order_id: str = "", client_order_id: str = "") -> dict[str, Any]:
        params = self._order_identity(symbol, order_id=order_id, client_order_id=client_order_id)
        return self.signed("DELETE", "/fapi/v1/order", params, order_operation=True)

    def create_listen_key(self) -> str:
        payload = self.api_key_request("POST", "/fapi/v1/listenKey")
        value = str(payload.get("listenKey") or "")
        if not value:
            raise BinanceApiError("Binance 未返回 listenKey")
        return value

    def keepalive_listen_key(self, listen_key: str) -> None:
        self.api_key_request("PUT", "/fapi/v1/listenKey", {"listenKey": listen_key})

    def close_listen_key(self, listen_key: str) -> None:
        self.api_key_request("DELETE", "/fapi/v1/listenKey", {"listenKey": listen_key})

    def public(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request(method, path, params or {}, signed=False, api_key=False).data

    def api_key_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        self._require_api_key()
        return self._request(method, path, params or {}, signed=False, api_key=True).data

    def signed(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        order_operation: bool = False,
    ) -> Any:
        self._require_api_key()
        if not self.api_secret:
            raise BinanceApiError("Binance API Secret 未配置")
        values = dict(params or {})
        values.setdefault("recvWindow", self.recv_window_ms)
        values.setdefault("timestamp", self._timestamp_ms())
        query = encode_query(values)
        values["signature"] = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            return self._request(method, path, values, signed=True, api_key=True).data
        except BinanceApiError as exc:
            if exc.code == -1021:
                self.synchronize_clock()
                values["timestamp"] = self._timestamp_ms()
                unsigned = {key: value for key, value in values.items() if key != "signature"}
                values["signature"] = hmac.new(
                    self.api_secret.encode(), encode_query(unsigned).encode(), hashlib.sha256
                ).hexdigest()
                return self._request(method, path, values, signed=True, api_key=True).data
            if order_operation and (exc.status is None or exc.status >= 500):
                raise BinanceApiError(
                    str(exc), code=exc.code, status=exc.status, retry_after=exc.retry_after, outcome_unknown=True
                ) from exc
            raise

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool,
        api_key: bool,
    ) -> BinanceResponse:
        headers = {"Accept": "application/json", "User-Agent": "CrossHedge/1"}
        if api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        try:
            return self._transport(method.upper(), self.base_url + path, params, headers, self.timeout)
        except BinanceApiError:
            raise
        except Exception as exc:
            raise BinanceApiError(f"Binance 请求失败: {exc}", outcome_unknown=signed) from exc

    @staticmethod
    def _urlopen_transport(
        method: str,
        url: str,
        params: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> BinanceResponse:
        query = encode_query(params)
        target = f"{url}?{query}" if query else url
        req = request.Request(target, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else {}
                return BinanceResponse(data, int(response.status), dict(response.headers.items()))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            message = str(payload.get("msg") or raw or exc.reason)
            code = payload.get("code")
            retry_header = exc.headers.get("Retry-After") if exc.headers else None
            retry_after = float(retry_header) if retry_header else None
            raise BinanceApiError(
                f"Binance API {code}: {message}",
                code=int(code) if code is not None else None,
                status=int(exc.code),
                retry_after=retry_after,
                outcome_unknown=int(exc.code) >= 500,
            ) from exc

    @staticmethod
    def _local_time_ms() -> int:
        return time.time_ns() // 1_000_000

    def _timestamp_ms(self) -> int:
        with self._lock:
            offset = self._clock_offset_ms
        return self._local_time_ms() + offset

    def _require_api_key(self) -> None:
        if not self.api_key:
            raise BinanceApiError("Binance API Key 未配置")

    @staticmethod
    def _order_identity(symbol: str, *, order_id: str, client_order_id: str) -> dict[str, Any]:
        if not order_id and not client_order_id:
            raise ValueError("order_id 和 client_order_id 至少提供一个")
        params: dict[str, Any] = {"symbol": normalize_symbol(symbol)}
        if order_id:
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = client_order_id
        return params


def normalize_symbol(symbol: str | None) -> str:
    value = str(symbol or "").strip().upper()
    if ":" in value:
        value = value.split(":", 1)[1]
    if value.endswith("-PERP"):
        value = value[:-5]
    return value.replace("/", "").replace("-", "").replace("_", "")


def encode_query(params: dict[str, Any]) -> str:
    cleaned = [(key, _parameter_text(value)) for key, value in params.items() if value is not None]
    return parse.urlencode(cleaned)


def _parameter_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
