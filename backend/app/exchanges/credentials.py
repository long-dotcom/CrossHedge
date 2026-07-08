"""
交易所凭证管理模块
==================

提供交易所 API 凭证的加密存储、解密读取、格式校验和连通性验证功能。
使用 Fernet 对称加密保护敏感凭证，加密密钥从配置中派生。

主要功能：
- 凭证加密 / 解密（Fernet）
- 凭证指纹计算（脱敏后 SHA256）
- 凭证 CRUD 操作
- Binance 账户 / 持仓 / 资金费 / 探针下单（通过 NautilusTrader）
- 凭证字段 schema 查询

使用方式::

    from app.exchanges.credentials import encrypt_credentials, decrypt_credentials
    encrypted = encrypt_credentials({"api_key": "xxx", "api_secret": "yyy"})
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from threading import Thread
from typing import Any

from sqlalchemy.orm import Session

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ExchangeCredential


logger = get_logger(__name__)

# 敏感字段名关键词列表，用于凭证脱敏和指纹计算
SENSITIVE_KEY_HINTS = ("key", "secret", "password", "passphrase", "token")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def normalize_venue(value: str) -> str:
    """规范化交易所名称：去除空白并转小写"""
    return (value or "").strip().lower()


# ---------------------------------------------------------------------------
# 凭证加密 / 解密
# ---------------------------------------------------------------------------

def encrypt_credentials(payload: dict[str, Any]) -> str:
    """使用 Fernet 加密凭证字典，返回加密后的 ASCII 字符串。"""
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_credentials(row: ExchangeCredential) -> dict[str, Any]:
    """解密交易所凭证行，返回原始凭证字典。

    异常:
        ValueError: 解密失败时抛出，通常因加密密钥变更导致。
    """
    from cryptography.fernet import InvalidToken
    if not row.encrypted_credentials:
        return {}
    try:
        raw = _fernet().decrypt(row.encrypted_credentials.encode("ascii"))
    except InvalidToken as exc:
        raise ValueError("交易所凭证解密失败，请检查 EXCHANGE_CONFIG_SECRET/JWT_SECRET 是否变更") from exc
    return json.loads(raw.decode("utf-8"))


def credential_fingerprint(payload: dict[str, Any]) -> str:
    """计算凭证指纹：脱敏后 JSON 的 SHA256 前 16 位。"""
    redacted_basis = json.dumps(_redacted(payload), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(redacted_basis.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 凭证展示 / CRUD
# ---------------------------------------------------------------------------

def public_exchange_credential(row: ExchangeCredential, *, include_schema: bool = True) -> dict[str, Any]:
    """将交易所凭证行序列化为前端可安全展示的字典（不含敏感数据）。"""
    data = {
        "id": row.id,
        "venue": row.venue,
        "display_name": row.display_name,
        "environment": row.environment,
        "enabled": row.enabled,
        "read_only": row.read_only,
        "configured": bool(row.encrypted_credentials),
        "credentials_fingerprint": row.credentials_fingerprint,
        "last_test_status": row.last_test_status,
        "last_test_message": row.last_test_message,
        "last_tested_at": row.last_tested_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if include_schema:
        data["credential_fields"] = credential_fields_for_venue(row.venue)
    return data


def upsert_exchange_credential(db: Session, payload: dict[str, Any]) -> ExchangeCredential:
    """创建或更新交易所凭证记录。

    参数:
        db: 数据库会话
        payload: 包含 venue / display_name / environment / enabled / credentials 等字段的字典
    """
    venue = normalize_venue(str(payload.get("venue") or ""))
    if not venue:
        raise ValueError("venue 不能为空")
    row = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue).first()
    if not row:
        row = ExchangeCredential(venue=venue)
    row.display_name = str(payload.get("display_name") or venue.upper()).strip()
    row.environment = str(payload.get("environment") or "sandbox").strip().lower()
    row.enabled = bool(payload.get("enabled", False))
    row.read_only = bool(payload.get("read_only", True))
    credentials = _clean_credentials(payload.get("credentials"))
    if credentials:
        row.encrypted_credentials = encrypt_credentials(credentials)
        row.credentials_fingerprint = credential_fingerprint(credentials)
        row.last_test_status = "untested"
        row.last_test_message = ""
        row.last_tested_at = None
    db.add(row)
    return row


def mark_test_result(row: ExchangeCredential, status: str, message: str) -> None:
    """标记交易所凭证的连通性测试结果。"""
    row.last_test_status = status
    row.last_test_message = message
    row.last_tested_at = utc_now()


def validate_exchange_credential(row: ExchangeCredential) -> tuple[str, str]:
    """验证交易所凭证的完整性和连通性。

    返回 (状态, 消息) 元组，状态包括 "ok" / "failed" / "warning"。
    """
    credentials = decrypt_credentials(row)
    missing = _missing_required_fields(row.venue, credentials)
    if missing:
        return "failed", f"缺少必填字段: {', '.join(missing)}"
    if row.venue not in {"mt5", "hyperliquid"}:
        return _validate_nautilus_runtime(row)
    try:
        import nautilus_trader.adapters  # noqa: F401
    except Exception as exc:
        return "failed", f"NautilusTrader adapter 依赖不可用: {exc}"
    return "warning", f"{row.venue} 凭证格式有效；真实账户连通性验证尚未接入"


# ---------------------------------------------------------------------------
# Binance 数据读取（通过 NautilusTrader）
# ---------------------------------------------------------------------------

def binance_account_balances(row: ExchangeCredential) -> dict[str, float]:
    """获取 Binance 现货账户余额"""
    account = _run_async(_nautilus_binance_spot_account(row))
    balances = getattr(account, "balances", []) or []
    totals: dict[str, float] = {}
    for item in balances:
        asset = str(getattr(item, "asset", "") or "")
        free = _float(getattr(item, "free", 0.0))
        locked = _float(getattr(item, "locked", 0.0))
        amount = free + locked
        if amount > 0:
            totals[asset] = amount
    return totals


def binance_futures_account(row: ExchangeCredential) -> dict[str, Any]:
    """获取 Binance 期货账户信息"""
    account = _run_async(_nautilus_binance_futures_account(row))
    return {field: getattr(account, field, None) for field in getattr(account, "__struct_fields__", ())}


def binance_futures_positions(row: ExchangeCredential) -> list[dict[str, Any]]:
    """获取 Binance 期货持仓列表"""
    payload = _run_async(_nautilus_binance_futures_positions(row))
    positions = []
    for item in payload if isinstance(payload, list) else []:
        amount = _float(getattr(item, "positionAmt", 0.0))
        if abs(amount) <= 0:
            continue
        mark_price = _float(getattr(item, "markPrice", 0.0))
        entry_price = _float(getattr(item, "entryPrice", 0.0))
        positions.append(
            {
                "platform": "binance",
                "symbol": str(getattr(item, "symbol", "") or ""),
                "side": "long" if amount > 0 else "short",
                "quantity": abs(amount),
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": _float(getattr(item, "unRealizedProfit", 0.0)),
                "margin_used": _float(getattr(item, "initialMargin", 0.0)) or abs(amount * mark_price),
                "liquidation_price": _optional_float(getattr(item, "liquidationPrice", None)),
            }
        )
    return positions


def binance_ticker_book(row: ExchangeCredential, symbol: str) -> dict[str, float]:
    """获取 Binance 期货 Ticker 数据"""
    ticker = _run_async(_nautilus_binance_futures_ticker(row, symbol))
    bid = _float(getattr(ticker, "bidPrice", 0.0))
    ask = _float(getattr(ticker, "askPrice", 0.0))
    bid_qty = _float(getattr(ticker, "bidQty", 0.0))
    ask_qty = _float(getattr(ticker, "askQty", 0.0))
    return {
        "bid": bid,
        "ask": ask,
        "depth_notional": min(bid * bid_qty, ask * ask_qty) if bid > 0 and ask > 0 else 0.0,
    }


def binance_futures_funding_history(row: ExchangeCredential, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """获取 Binance 期货资金费历史"""
    rows = _run_async(_nautilus_binance_futures_funding_history(row, symbol, start_ms, end_ms))
    return [
        {
            "symbol": str(item.get("symbol") or symbol),
            "fundingRate": item.get("fundingRate"),
            "fundingTime": item.get("fundingTime"),
        }
        for item in rows
        if isinstance(item, dict)
    ]


def binance_futures_probe_order(row: ExchangeCredential, order: AdapterOrder, *, configured_min_base_size: float = 0.0) -> AdapterOrderResult:
    """Binance 期货 paper-live 探针下单"""
    try:
        payload = _run_async(_nautilus_binance_futures_probe_order(row, order, configured_min_base_size))
    except Exception as exc:
        return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, f"Binance Nautilus paper-live 探针下单失败: {exc}")
    status = str(payload.get("status") or "").lower()
    order_id = str(payload.get("orderId") or payload.get("clientOrderId") or "")
    executed_qty = _decimal(payload.get("executedQty"))
    avg_price = _decimal(payload.get("avgPrice"))
    cum_quote = _decimal(payload.get("cumQuote"))
    # 无均价时通过累计成交额 / 成交量计算
    if avg_price <= 0 and executed_qty > 0 and cum_quote > 0:
        avg_price = cum_quote / executed_qty
    if status in {"filled", "partially_filled"} and executed_qty > 0 and avg_price > 0:
        return AdapterOrderResult(
            True,
            order_id,
            status,
            float(order.quantity),
            float(avg_price),
            0.0,
            f"Binance paper-live 探针真实成交量 {payload.get('executedQty')}，paper 账本成交量 {order.quantity}",
        )
    return AdapterOrderResult(False, order_id, status or "rejected", 0.0, 0.0, 0.0, f"Binance paper-live 探针未立即成交: {payload}")


def binance_futures_live_order(row: ExchangeCredential, order: AdapterOrder) -> AdapterOrderResult:
    """Binance 期货 live 真实下单。"""
    try:
        payload = _run_async(_nautilus_binance_futures_live_order(row, order))
    except Exception as exc:
        return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, f"Binance Nautilus live 下单失败: {exc}")
    status = str(payload.get("status") or "").lower()
    order_id = str(payload.get("orderId") or payload.get("clientOrderId") or "")
    executed_qty = _decimal(payload.get("executedQty"))
    avg_price = _decimal(payload.get("avgPrice"))
    cum_quote = _decimal(payload.get("cumQuote"))
    if avg_price <= 0 and executed_qty > 0 and cum_quote > 0:
        avg_price = cum_quote / executed_qty
    if status in {"filled", "partially_filled"} and executed_qty > 0 and avg_price > 0:
        return AdapterOrderResult(
            True,
            order_id,
            status,
            float(executed_qty),
            float(avg_price),
            0.0,
            f"Binance Nautilus live 真实成交量 {executed_qty}",
        )
    if status in {"new", "accepted", "pending_new"}:
        return AdapterOrderResult(True, order_id, status, 0.0, 0.0, 0.0, "Binance Nautilus live 订单已提交，等待成交")
    return AdapterOrderResult(False, order_id, status or "rejected", 0.0, 0.0, 0.0, f"Binance Nautilus live 下单未成交: {payload}")


# ---------------------------------------------------------------------------
# 凭证字段 schema
# ---------------------------------------------------------------------------

def credential_fields_for_venue(venue: str) -> list[dict[str, Any]]:
    """返回指定交易所的凭证字段定义列表。"""
    venue = normalize_venue(venue)
    if venue == "okx":
        return [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "api_secret", "label": "API Secret", "secret": True, "required": True},
            {"name": "passphrase", "label": "Passphrase", "secret": True, "required": True},
        ]
    if venue in {"binance", "bybit", "kraken"}:
        return [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "api_secret", "label": "API Secret", "secret": True, "required": True},
        ]
    if venue == "hyperliquid":
        return [
            {"name": "account_address", "label": "Account Address", "secret": False, "required": True},
            {"name": "secret_key", "label": "Secret Key", "secret": True, "required": False},
        ]
    if venue == "mt5":
        return [
            {"name": "login", "label": "Login", "secret": False, "required": True},
            {"name": "password", "label": "Password", "secret": True, "required": True},
            {"name": "server", "label": "Server", "secret": False, "required": True},
        ]
    return [
        {"name": "api_key", "label": "API Key", "secret": True, "required": False},
        {"name": "api_secret", "label": "API Secret", "secret": True, "required": False},
    ]


def _missing_required_fields(venue: str, credentials: dict[str, Any]) -> list[str]:
    """检查凭证中是否缺少必填字段"""
    missing = []
    for field in credential_fields_for_venue(venue):
        if field.get("required") and not str(credentials.get(str(field["name"])) or "").strip():
            missing.append(str(field["name"]))
    return missing


# ---------------------------------------------------------------------------
# Binance 验证
# ---------------------------------------------------------------------------

def _validate_binance(row: ExchangeCredential, credentials: dict[str, Any]) -> tuple[str, str]:
    """验证 Binance 凭证的连通性和交易权限"""
    try:
        payload = _run_async(_nautilus_binance_futures_account(row, credentials))
        can_trade = getattr(payload, "canTrade", None)
        assets = getattr(payload, "assets", []) or []
        wallet = getattr(payload, "totalWalletBalance", "")
        return "ok", f"Binance {row.environment} Futures 验证成功: canTrade={can_trade}, assets={len(assets)}, wallet={wallet}"
    except RuntimeError as futures_exc:
        futures_message = str(futures_exc)
    # 期货验证失败时回退到现货验证
    try:
        payload = _run_async(_nautilus_binance_spot_account(row, credentials))
    except RuntimeError as exc:
        return "failed", f"Futures: {futures_message}; Spot: {exc}"
    can_trade = getattr(payload, "canTrade", None)
    account_type = getattr(payload, "accountType", "spot")
    return "ok", f"Binance {row.environment} 账户验证成功: accountType={account_type}, canTrade={can_trade}"


def _validate_nautilus_runtime(row: ExchangeCredential) -> tuple[str, str]:
    """通过 Nautilus TradingNode runtime 验证非原生交易所。"""
    try:
        from app.adapters.nautilus_runtime import nautilus_live_supported, nautilus_runtime_manager

        if not nautilus_live_supported(row.venue):
            return "failed", f"{row.venue} 尚未接入 Nautilus live runtime"
        runtime = nautilus_runtime_manager.runtime_for(row)
        try:
            account = runtime.get_account()
        except RuntimeError as exc:
            message = str(exc)
            if "账户状态尚未加载" in message:
                return "warning", f"{row.venue} Nautilus runtime 已启动，账户状态仍在同步中"
            raise
        return "ok", f"{row.venue} Nautilus runtime 验证成功: account_fields={len(account)}"
    except Exception as exc:
        return "failed", f"{row.venue} Nautilus runtime 验证失败: {exc}"


# ---------------------------------------------------------------------------
# Nautilus Binance 异步实现
# ---------------------------------------------------------------------------

async def _nautilus_binance_futures_account(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    """通过 Nautilus 查询 Binance 期货账户信息"""
    account_api, _ = _nautilus_binance_futures_apis(row, credentials)
    try:
        return await account_api.query_futures_account_info()
    except Exception as exc:
        raise RuntimeError(f"nautilus futures account {exc}") from exc


async def _nautilus_binance_futures_positions(row: ExchangeCredential):
    """通过 Nautilus 查询 Binance 期货持仓"""
    account_api, _ = _nautilus_binance_futures_apis(row)
    try:
        return await account_api.query_futures_position_risk()
    except Exception as exc:
        raise RuntimeError(f"nautilus futures positions {exc}") from exc


async def _nautilus_binance_futures_ticker(row: ExchangeCredential, symbol: str):
    """通过 Nautilus 查询 Binance 期货 Ticker"""
    _, market_api = _nautilus_binance_futures_apis(row, credentials={})
    try:
        rows = await market_api.query_ticker_book(symbol=symbol)
    except Exception as exc:
        raise RuntimeError(f"nautilus futures ticker {exc}") from exc
    if not rows:
        raise RuntimeError(f"nautilus futures ticker empty: {symbol}")
    return rows[0]


async def _nautilus_binance_futures_funding_history(row: ExchangeCredential, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """通过 Nautilus 查询 Binance 期货资金费历史（自定义端点）"""
    import msgspec
    from nautilus_trader.adapters.binance.common.enums import BinanceSecurityType
    from nautilus_trader.adapters.binance.http.endpoint import BinanceHttpEndpoint
    from nautilus_trader.core.nautilus_pyo3 import HttpMethod

    _, market_api = _nautilus_binance_futures_apis(row, credentials={})

    class FundingRateEndpoint(BinanceHttpEndpoint):
        class GetParameters(msgspec.Struct, omit_defaults=True, frozen=True):
            symbol: str
            startTime: int | None = None
            endTime: int | None = None
            limit: int | None = None

        def __init__(self):
            super().__init__(
                market_api.client,
                {HttpMethod.GET: BinanceSecurityType.NONE},
                market_api.base_endpoint + "fundingRate",
            )
            self._get_resp_decoder = msgspec.json.Decoder()

        async def get(self, params: GetParameters) -> list[dict[str, Any]]:
            raw = await self._method(HttpMethod.GET, params)
            decoded = self._get_resp_decoder.decode(raw)
            return decoded if isinstance(decoded, list) else []

    endpoint = FundingRateEndpoint()
    try:
        return await endpoint.get(FundingRateEndpoint.GetParameters(symbol=_binance_symbol(symbol), startTime=start_ms, endTime=end_ms, limit=1000))
    except Exception as exc:
        raise RuntimeError(f"nautilus futures funding {exc}") from exc


async def _nautilus_binance_futures_probe_order(row: ExchangeCredential, order: AdapterOrder, configured_min_base_size: float) -> dict[str, Any]:
    """通过 Nautilus 发送 Binance 期货 paper-live 探针订单"""
    from nautilus_trader.core.nautilus_pyo3 import HttpMethod

    _, market_api = _nautilus_binance_futures_apis(row)
    symbol = _binance_symbol(order.venue_symbol or order.symbol)
    specs = await _binance_futures_symbol_specs(market_api, symbol)
    ticker = await _nautilus_binance_futures_ticker(row, symbol)
    bid = _decimal(getattr(ticker, "bidPrice", 0))
    ask = _decimal(getattr(ticker, "askPrice", 0))
    mid = ((bid + ask) / Decimal("2")) if bid > 0 and ask > 0 else Decimal("0")
    quantity = _binance_probe_quantity(specs, mid, configured_min_base_size)
    payload = {
        "symbol": symbol,
        "side": "BUY" if str(order.side).lower() in {"buy", "long"} else "SELL",
        "type": "MARKET",
        "quantity": _decimal_text(quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": f"mt5h_probe_{int(utc_now().replace(tzinfo=timezone.utc).timestamp() * 1000)}"[:36],
        "recvWindow": "5000",
    }
    if order.reduce_only:
        payload["reduceOnly"] = "true"
    raw = await market_api.client.sign_request(HttpMethod.POST, market_api.base_endpoint + "order", payload=payload, ratelimiter_keys=["REQUEST_WEIGHT", "ORDERS"])
    decoded = _decode_json_payload(raw)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"unexpected order response: {decoded}")
    return decoded


async def _nautilus_binance_futures_live_order(row: ExchangeCredential, order: AdapterOrder) -> dict[str, Any]:
    """通过 Nautilus 发送 Binance 期货 live 订单。"""
    from nautilus_trader.core.nautilus_pyo3 import HttpMethod

    _, market_api = _nautilus_binance_futures_apis(row)
    symbol = _binance_symbol(order.venue_symbol or order.symbol)
    specs = await _binance_futures_symbol_specs(market_api, symbol)
    ticker = await _nautilus_binance_futures_ticker(row, symbol)
    bid = _decimal(getattr(ticker, "bidPrice", 0))
    ask = _decimal(getattr(ticker, "askPrice", 0))
    mid = ((bid + ask) / Decimal("2")) if bid > 0 and ask > 0 else Decimal("0")
    quantity = _binance_live_quantity(specs, _decimal(order.quantity), mid)
    order_type = str(order.order_type or "market").strip().lower()
    payload = {
        "symbol": symbol,
        "side": "BUY" if str(order.side).lower() in {"buy", "long"} else "SELL",
        "quantity": _decimal_text(quantity),
        "newOrderRespType": "RESULT",
        "newClientOrderId": f"mt5h_live_{int(utc_now().replace(tzinfo=timezone.utc).timestamp() * 1000)}"[:36],
        "recvWindow": "5000",
    }
    if order.reduce_only:
        payload["reduceOnly"] = "true"
    if order_type == "market":
        if order.post_only:
            raise RuntimeError("Binance futures market 订单不支持 post_only")
        payload["type"] = "MARKET"
    elif order_type == "limit":
        if order.price is None or _decimal(order.price) <= 0:
            raise RuntimeError("Binance futures limit 订单必须提供有效价格")
        payload["type"] = "LIMIT"
        payload["price"] = _decimal_text(_decimal(order.price))
        payload["timeInForce"] = "GTX" if order.post_only else "GTC"
    else:
        raise RuntimeError(f"Binance futures 不支持订单类型: {order.order_type}")
    raw = await market_api.client.sign_request(HttpMethod.POST, market_api.base_endpoint + "order", payload=payload, ratelimiter_keys=["REQUEST_WEIGHT", "ORDERS"])
    decoded = _decode_json_payload(raw)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"unexpected order response: {decoded}")
    return decoded


async def _binance_futures_symbol_specs(market_api, symbol: str) -> dict[str, Decimal]:
    """获取 Binance 期货品种的精度和最小下单量规格"""
    info = await market_api.query_futures_exchange_info()
    symbols = _field(info, "symbols", [])
    for item in symbols or []:
        if str(_field(item, "symbol", "")).upper() == symbol:
            return _parse_binance_futures_filters(item)
    raise RuntimeError(f"Binance futures symbol not found: {symbol}")


def _parse_binance_futures_filters(symbol_info: Any) -> dict[str, Decimal]:
    """解析 Binance 期货品种的过滤器，提取步长、最小数量和最小名义价值"""
    filters = _field(symbol_info, "filters", []) or []
    step = Decimal("0")
    min_qty = Decimal("0")
    min_notional = Decimal("0")
    for item in filters:
        filter_type = str(_field(item, "filterType", "") or "")
        if filter_type in {"MARKET_LOT_SIZE", "LOT_SIZE"}:
            item_step = _decimal(_field(item, "stepSize", 0))
            item_min_qty = _decimal(_field(item, "minQty", 0))
            if filter_type == "MARKET_LOT_SIZE" or step <= 0:
                step = item_step
            if item_min_qty > min_qty:
                min_qty = item_min_qty
        if filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            item_min_notional = max(_decimal(_field(item, "notional", 0)), _decimal(_field(item, "minNotional", 0)))
            if item_min_notional > min_notional:
                min_notional = item_min_notional
    precision_step = Decimal("1") / (Decimal("10") ** int(_field(symbol_info, "quantityPrecision", 0) or 0))
    if step <= 0:
        step = precision_step
    return {"step": step, "min_qty": min_qty, "min_notional": min_notional}


def _binance_probe_quantity(specs: dict[str, Decimal], mid_price: Decimal, configured_min_base_size: float) -> Decimal:
    """计算 Binance 探针下单数量（满足最小名义价值和最小下单量要求）"""
    step = specs.get("step", Decimal("0"))
    min_qty = max(specs.get("min_qty", Decimal("0")), _decimal(configured_min_base_size))
    min_notional = specs.get("min_notional", Decimal("0"))
    if mid_price > 0 and min_notional > 0:
        min_qty = max(min_qty, min_notional / mid_price)
    quantity = _ceil_to_step(min_qty if min_qty > 0 else step, step)
    if quantity <= 0:
        raise RuntimeError("Binance futures 最小探针数量无法计算")
    return quantity


def _binance_live_quantity(specs: dict[str, Decimal], requested: Decimal, mid_price: Decimal) -> Decimal:
    """校验并规整 Binance live 真实下单数量。"""
    if requested <= 0:
        raise RuntimeError("Binance futures live 下单数量必须大于 0")
    step = specs.get("step", Decimal("0"))
    min_qty = specs.get("min_qty", Decimal("0"))
    min_notional = specs.get("min_notional", Decimal("0"))
    quantity = _floor_to_step(requested, step)
    if quantity <= 0:
        raise RuntimeError("Binance futures live 下单数量无法计算")
    if min_qty > 0 and quantity < min_qty:
        raise RuntimeError(f"Binance futures live 下单数量低于最小数量: {quantity} < {min_qty}")
    if mid_price > 0 and min_notional > 0 and quantity * mid_price < min_notional:
        raise RuntimeError(f"Binance futures live 下单名义价值低于最小名义额: {quantity * mid_price} < {min_notional}")
    return quantity


async def _nautilus_binance_spot_account(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    """通过 Nautilus 查询 Binance 现货账户信息"""
    account_api = _nautilus_binance_spot_account_api(row, credentials)
    try:
        return await account_api.query_spot_account_info()
    except Exception as exc:
        raise RuntimeError(f"nautilus spot account {exc}") from exc


def _nautilus_binance_futures_apis(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    """构建 Binance 期货 HTTP API 客户端"""
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
    from nautilus_trader.adapters.binance.common.urls import get_http_base_url
    from nautilus_trader.adapters.binance.futures.http.account import BinanceFuturesAccountHttpAPI
    from nautilus_trader.adapters.binance.futures.http.market import BinanceFuturesMarketHttpAPI
    from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
    from nautilus_trader.common.component import LiveClock

    credentials = credentials if credentials is not None else decrypt_credentials(row)
    clock = LiveClock()
    account_type = BinanceAccountType.USDT_FUTURES
    client = BinanceHttpClient(
        clock=clock,
        api_key=str(credentials.get("api_key") or "") or None,
        api_secret=str(credentials.get("api_secret") or "") or None,
        base_url=get_http_base_url(account_type, _binance_environment(row.environment), is_us=False),
    )
    return BinanceFuturesAccountHttpAPI(client, clock, account_type), BinanceFuturesMarketHttpAPI(client, account_type)


def _nautilus_binance_spot_account_api(row: ExchangeCredential, credentials: dict[str, Any] | None = None):
    """构建 Binance 现货 HTTP API 客户端"""
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType
    from nautilus_trader.adapters.binance.common.urls import get_http_base_url
    from nautilus_trader.adapters.binance.http.client import BinanceHttpClient
    from nautilus_trader.adapters.binance.spot.http.account import BinanceSpotAccountHttpAPI
    from nautilus_trader.common.component import LiveClock

    credentials = credentials if credentials is not None else decrypt_credentials(row)
    clock = LiveClock()
    account_type = BinanceAccountType.SPOT
    client = BinanceHttpClient(
        clock=clock,
        api_key=str(credentials.get("api_key") or "") or None,
        api_secret=str(credentials.get("api_secret") or "") or None,
        base_url=get_http_base_url(account_type, _binance_environment(row.environment), is_us=False),
    )
    return BinanceSpotAccountHttpAPI(client, clock, account_type)


def _binance_environment(value: str):
    """将环境字符串映射为 Binance 环境枚举"""
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

    normalized = (value or "").strip().lower()
    if normalized in {"test", "testnet", "sandbox"}:
        return BinanceEnvironment.TESTNET
    if normalized == "demo":
        return BinanceEnvironment.DEMO
    return BinanceEnvironment.LIVE


def _binance_symbol(symbol: str) -> str:
    """规范化 Binance 品种符号（去除分隔符和前缀）"""
    value = str(symbol or "").strip().upper()
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.replace("/", "").replace("-", "").replace("_", "")


# ---------------------------------------------------------------------------
# 异步运行辅助
# ---------------------------------------------------------------------------

def _run_async(coro):
    """在同步上下文中运行异步协程。

    如果当前已有事件循环运行，则在新线程中执行；否则直接 asyncio.run。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:
            result["error"] = exc

    thread = Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


# ---------------------------------------------------------------------------
# 类型转换辅助
# ---------------------------------------------------------------------------

def _optional_float(value: Any) -> float | None:
    """安全转换为浮点数，无效值或 <= 0 时返回 None"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _field(value: Any, name: str, default: Any = None) -> Any:
    """从字典或对象中安全获取字段值"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any) -> Decimal:
    """安全转换为 Decimal"""
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向上取整到最近的步长倍数"""
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return units * step


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取整到最近的步长倍数，避免真实交易超过策略目标数量。"""
    if step <= 0:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def _decimal_text(value: Decimal) -> str:
    """将 Decimal 格式化为字符串"""
    return format(value.normalize(), "f")


def _float(value: Any) -> float:
    """安全转换为浮点数"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _decode_json_payload(raw: Any) -> Any:
    """解码交易所 JSON 响应，msgspec 不可用时回退标准库 json。"""
    if not isinstance(raw, (bytes, bytearray)):
        return raw
    try:
        import msgspec

        return msgspec.json.decode(raw)
    except ModuleNotFoundError:
        return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# 加密 / 脱敏内部函数
# ---------------------------------------------------------------------------

def _fernet() -> "Fernet":
    """构建 Fernet 加密实例。

    密钥来源优先级：
    1. settings.security.exchange_config_secret
    2. settings.security.jwt_secret
    """
    from cryptography.fernet import Fernet
    settings = get_settings()
    secret = (settings.security.exchange_config_secret or settings.security.jwt_secret).strip()
    if not secret:
        raise ValueError("缺少交易所配置加密密钥")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


def _redacted(value: Any) -> Any:
    """递归脱敏：将敏感字段的值替换为 '***'"""
    if isinstance(value, dict):
        return {key: ("***" if _is_sensitive_key(key) and val else _redacted(val)) for key, val in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否为敏感字段"""
    normalized = key.lower()
    return any(hint in normalized for hint in SENSITIVE_KEY_HINTS)


def _clean_credentials(value: Any) -> dict[str, Any]:
    """清洗凭证字典：去除空白和空值"""
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, str):
            item = item.strip()
        if item in ("", None):
            continue
        cleaned[str(key)] = item
    return cleaned
