"""交易所凭证管理和原生数据访问。

凭证加密后存入数据库；连通性、账户权限和环境校验由项目原生 Connector
完成。当前只允许 Hyperliquid、Binance USDⓈ-M Futures 和 MT5。
"""

from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.time_utils import utc_now
from app.db.models import ExchangeCredential
from app.venues.binance import BinanceFuturesConnector, BinanceFuturesRestClient
from app.venues.binance.rest import normalize_symbol
from app.venues.hyperliquid import HyperliquidConnector
from app.venues.mt5 import MT5Connector

SENSITIVE_KEY_HINTS = ("key", "secret", "password", "passphrase", "token")
SUPPORTED_VENUES = ("hyperliquid", "mt5", "binance")


def normalize_venue(value: str) -> str:
    return str(value or "").strip().lower()


def normalize_connection_environment(value: str) -> str:
    normalized = str(value or "live").strip().lower()
    if normalized in {"", "sandbox", "prod", "production"}:
        return "live"
    if normalized in {"test", "testnet"}:
        return "testnet"
    if normalized in {"demo", "live"}:
        return normalized
    raise ValueError(f"不支持的交易所连接环境: {value}")


def encrypt_credentials(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return _fernet().encrypt(raw).decode("ascii")


def decrypt_credentials(row: ExchangeCredential) -> dict[str, Any]:
    from cryptography.fernet import InvalidToken

    if not row.encrypted_credentials:
        return {}
    try:
        raw = _fernet().decrypt(row.encrypted_credentials.encode("ascii"))
    except InvalidToken as exc:
        raise ValueError("交易所凭证解密失败，请检查 EXCHANGE_CONFIG_SECRET/JWT_SECRET 是否变更") from exc
    return json.loads(raw.decode("utf-8"))


def credential_fingerprint(payload: dict[str, Any]) -> str:
    """只对字段存在性和脱敏值计算指纹，不泄漏明文。"""
    basis = json.dumps(_redacted(payload), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def public_exchange_credential(row: ExchangeCredential, *, include_schema: bool = True) -> dict[str, Any]:
    data = {
        "id": row.id,
        "venue": row.venue,
        "display_name": row.display_name,
        "environment": normalize_connection_environment(row.environment),
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
    venue = normalize_venue(str(payload.get("venue") or ""))
    if venue not in SUPPORTED_VENUES:
        raise ValueError(f"当前仅支持交易场所: {', '.join(SUPPORTED_VENUES)}")
    row = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue).first()
    if row is None:
        row = ExchangeCredential(venue=venue)
    row.display_name = str(payload.get("display_name") or venue.upper()).strip()
    row.environment = normalize_connection_environment(payload.get("environment") or "live")
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
    row.last_test_status = status
    row.last_test_message = message
    row.last_tested_at = utc_now()


def validate_exchange_credential(row: ExchangeCredential) -> tuple[str, str]:
    credentials = decrypt_credentials(row)
    missing = _missing_required_fields(row.venue, credentials, read_only=bool(row.read_only))
    if missing:
        return "failed", f"缺少必填字段: {', '.join(missing)}"
    try:
        connector = build_credential_connector(row, credentials=credentials)
        result = connector.validate_credentials()
    except Exception as exc:
        return "failed", f"{row.venue} 原生凭证验证失败: {exc}"
    details = "; ".join(f"{item.name}={'ok' if item.ok else 'failed'}({item.message})" for item in result.items)
    if result.valid and result.can_read:
        status = "ok" if row.read_only or result.can_trade else "warning"
    else:
        status = "failed"
    return status, details


def build_credential_connector(
    row: ExchangeCredential,
    *,
    credentials: dict[str, Any] | None = None,
):
    values = credentials if credentials is not None else decrypt_credentials(row)
    common = {
        "credentials": values,
        "environment": normalize_connection_environment(row.environment),
        "read_only": bool(row.read_only),
    }
    if row.venue == "binance":
        return BinanceFuturesConnector(**common)
    if row.venue == "hyperliquid":
        return HyperliquidConnector(**common)
    if row.venue == "mt5":
        return MT5Connector(**common)
    raise ValueError(f"尚未接入原生交易场所: {row.venue}")


def binance_account_balances(row: ExchangeCredential) -> dict[str, float]:
    account = _binance_client(row).account()
    return {
        str(item.get("asset") or ""): float(item.get("walletBalance") or 0)
        for item in account.get("assets", [])
        if float(item.get("walletBalance") or 0) != 0
    }


def binance_futures_account(row: ExchangeCredential) -> dict[str, Any]:
    return _binance_client(row).account()


def binance_futures_positions(row: ExchangeCredential) -> list[dict[str, Any]]:
    rows = []
    for item in _binance_client(row).position_risk():
        amount = float(item.get("positionAmt") or 0)
        if amount == 0:
            continue
        rows.append(
            {
                "platform": "binance",
                "symbol": str(item.get("symbol") or ""),
                "side": "long" if amount > 0 else "short",
                "position_side": str(item.get("positionSide") or "BOTH"),
                "quantity": abs(amount),
                "entry_price": float(item.get("entryPrice") or 0),
                "mark_price": float(item.get("markPrice") or 0),
                "unrealized_pnl": float(item.get("unRealizedProfit") or 0),
                "margin_used": float(item.get("initialMargin") or 0),
                "liquidation_price": _optional_float(item.get("liquidationPrice")),
            }
        )
    return rows


def binance_ticker_book(row: ExchangeCredential, symbol: str) -> dict[str, float]:
    data = _binance_client(row).book_ticker(symbol)
    bid = float(data.get("bidPrice") or 0)
    ask = float(data.get("askPrice") or 0)
    bid_quantity = float(data.get("bidQty") or 0)
    ask_quantity = float(data.get("askQty") or 0)
    return {
        "bid": bid,
        "ask": ask,
        "depth_notional": min(bid * bid_quantity, ask * ask_quantity) if bid > 0 and ask > 0 else 0.0,
    }


def binance_futures_funding_history(
    row: ExchangeCredential,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, Any]]:
    return _binance_client(row).funding_history(symbol, start_ms, end_ms)


def binance_futures_current_funding(row: ExchangeCredential, symbol: str) -> dict[str, Any]:
    client = _binance_client(row)
    normalized = normalize_symbol(symbol)
    premium = client.premium_index(normalized)
    interval_hours = 8.0
    for item in client.funding_info():
        if normalize_symbol(item.get("symbol")) == normalized:
            interval_hours = float(item.get("fundingIntervalHours") or interval_hours)
            break
    return {
        "symbol": normalized,
        "funding_rate": float(premium.get("lastFundingRate") or 0),
        "funding_interval_hours": interval_hours,
        "next_funding_time_ms": int(premium.get("nextFundingTime") or 0),
        "source": "binance_native_premium_index",
    }


def binance_futures_commission_rate(row: ExchangeCredential, symbol: str) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    data = _binance_client(row).commission_rate(normalized)
    return {
        "symbol": normalized,
        "maker_fee_rate": float(data.get("makerCommissionRate") or 0),
        "taker_fee_rate": float(data.get("takerCommissionRate") or 0),
        "source": "binance_native_commission_rate",
    }


def credential_fields_for_venue(venue: str) -> list[dict[str, Any]]:
    normalized = normalize_venue(venue)
    if normalized == "binance":
        return [
            {"name": "api_key", "label": "API Key", "secret": True, "required": True},
            {"name": "api_secret", "label": "API Secret", "secret": True, "required": True},
        ]
    if normalized == "hyperliquid":
        return [
            {"name": "account_address", "label": "Account Address", "secret": False, "required": True},
            {"name": "secret_key", "label": "Signer Secret Key", "secret": True, "required": False},
        ]
    if normalized == "mt5":
        return [
            {"name": "login", "label": "Login", "secret": False, "required": True},
            {"name": "password", "label": "Password", "secret": True, "required": True},
            {"name": "server", "label": "Server", "secret": False, "required": True},
        ]
    return []


def _missing_required_fields(
    venue: str,
    credentials: dict[str, Any],
    *,
    read_only: bool = False,
) -> list[str]:
    missing = []
    for field in credential_fields_for_venue(venue):
        required = bool(field.get("required"))
        if field.get("name") == "secret_key" and not read_only:
            required = True
        if required and not str(credentials.get(str(field["name"])) or "").strip():
            missing.append(str(field["name"]))
    return missing


def _binance_client(row: ExchangeCredential) -> BinanceFuturesRestClient:
    credentials = decrypt_credentials(row)
    return BinanceFuturesRestClient(
        api_key=str(credentials.get("api_key") or ""),
        api_secret=str(credentials.get("api_secret") or ""),
        environment=normalize_connection_environment(row.environment),
    )


def _parse_binance_futures_filters(symbol_info: Any) -> dict[str, Decimal]:
    filters = _field(symbol_info, "filters", []) or []
    step = Decimal("0")
    min_quantity = Decimal("0")
    min_notional = Decimal("0")
    for item in filters:
        filter_type = str(_field(item, "filterType", "") or "")
        if filter_type in {"MARKET_LOT_SIZE", "LOT_SIZE"}:
            item_step = _decimal(_field(item, "stepSize", 0))
            item_minimum = _decimal(_field(item, "minQty", 0))
            if filter_type == "MARKET_LOT_SIZE" or step <= 0:
                step = item_step
            min_quantity = max(min_quantity, item_minimum)
        elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
            min_notional = max(
                min_notional,
                _decimal(_field(item, "notional", 0)),
                _decimal(_field(item, "minNotional", 0)),
            )
    if step <= 0:
        precision = int(_field(symbol_info, "quantityPrecision", 0) or 0)
        step = Decimal("1") / (Decimal("10") ** precision)
    return {"step": step, "min_qty": min_quantity, "min_notional": min_notional}


def _binance_probe_quantity(
    specs: dict[str, Decimal],
    mid_price: Decimal,
    configured_min_base_size: float,
) -> Decimal:
    step = specs.get("step", Decimal("0"))
    minimum = max(specs.get("min_qty", Decimal("0")), _decimal(configured_min_base_size))
    min_notional = specs.get("min_notional", Decimal("0"))
    if mid_price > 0 and min_notional > 0:
        minimum = max(minimum, min_notional / mid_price)
    quantity = _ceil_to_step(minimum if minimum > 0 else step, step)
    if quantity <= 0:
        raise RuntimeError("Binance Futures 最小探针数量无法计算")
    return quantity


def _binance_live_quantity(
    specs: dict[str, Decimal],
    requested: Decimal,
    mid_price: Decimal,
) -> Decimal:
    if requested <= 0:
        raise RuntimeError("Binance Futures live 下单数量必须大于 0")
    step = specs.get("step", Decimal("0"))
    quantity = _floor_to_step(requested, step)
    min_quantity = specs.get("min_qty", Decimal("0"))
    min_notional = specs.get("min_notional", Decimal("0"))
    if quantity <= 0:
        raise RuntimeError("Binance Futures live 下单数量无法计算")
    if min_quantity > 0 and quantity < min_quantity:
        raise RuntimeError(f"Binance Futures live 下单数量低于最小数量: {quantity} < {min_quantity}")
    if mid_price > 0 and min_notional > 0 and quantity * mid_price < min_notional:
        raise RuntimeError(f"Binance Futures live 下单名义价值低于最小名义额: {quantity * mid_price} < {min_notional}")
    return quantity


def _binance_environment(value: str) -> str:
    """保留旧内部函数名，但只返回项目原生环境字符串。"""
    return normalize_connection_environment(value)


def _binance_symbol(symbol: str) -> str:
    return normalize_symbol(symbol)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _fernet():
    from cryptography.fernet import Fernet

    settings = get_settings()
    configured = str(getattr(settings.security, "exchange_config_secret", "") or "").strip()
    source = configured or str(settings.security.jwt_secret or "crosshedge-exchange-config")
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("***" if _is_sensitive_key(str(key)) and item not in (None, "") else _redacted(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(hint in normalized for hint in SENSITIVE_KEY_HINTS)


def _clean_credentials(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if item not in (None, "")}
