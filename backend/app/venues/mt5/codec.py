"""MT5 Gateway JSON 协议与领域模型编解码。"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypeVar

from app.venues.domain.events import VenueEvent, VenueEventType
from app.core.time_utils import utc_now
from app.venues.domain.models import (
    AccountSnapshot, Balance, CredentialCheck, CredentialCheckItem, Fill,
    Instrument, OrderBookSnapshot, OrderRequest, OrderSnapshot, OrderStatus,
    OrderType, Position, PositionSide, Side, Ticker, TimeInForce,
)

T = TypeVar("T")


def primitive(value: Any) -> Any:
    """将领域对象转换为可跨语言传输的 JSON 值。"""
    if dataclasses.is_dataclass(value):
        return {field.name: primitive(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [primitive(item) for item in value]
    if isinstance(value, list):
        return [primitive(item) for item in value]
    if isinstance(value, dict):
        return {str(key): primitive(item) for key, item in value.items()}
    return value


def dumps(value: Any) -> str:
    return json.dumps(primitive(value), ensure_ascii=False, separators=(",", ":"))


def loads(value: str | bytes | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _dt(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value or "0"))


def balance(data: dict[str, Any]) -> Balance:
    return Balance(data["asset"], _decimal(data["wallet_balance"]), _decimal(data["available_balance"]), _decimal(data.get("locked_balance")))


def account(data: dict[str, Any]) -> AccountSnapshot:
    return AccountSnapshot(
        venue=data["venue"], account_id=str(data["account_id"]), currency=data["currency"],
        equity=_decimal(data["equity"]), available_balance=_decimal(data["available_balance"]),
        margin_used=_decimal(data["margin_used"]), maintenance_margin=_decimal(data.get("maintenance_margin")),
        balances=tuple(balance(item) for item in data.get("balances", [])),
        observed_at=_dt(data.get("observed_at")) or utc_now(), raw=data.get("raw", {}),
    )


def position(data: dict[str, Any]) -> Position:
    return Position(
        venue=data["venue"], account_id=str(data["account_id"]), symbol=data["symbol"],
        position_side=PositionSide(data["position_side"]), quantity=_decimal(data["quantity"]),
        entry_price=_decimal(data["entry_price"]), mark_price=_decimal(data["mark_price"]),
        unrealized_pnl=_decimal(data.get("unrealized_pnl")), realized_pnl=_decimal(data.get("realized_pnl")),
        margin_used=_decimal(data.get("margin_used")),
        liquidation_price=_decimal(data["liquidation_price"]) if data.get("liquidation_price") is not None else None,
        external_position_id=str(data.get("external_position_id", "")),
        observed_at=_dt(data.get("observed_at")) or utc_now(), raw=data.get("raw", {}),
    )


def instrument(data: dict[str, Any]) -> Instrument:
    decimal_fields = ("quantity_step", "minimum_quantity", "price_tick", "minimum_notional", "contract_size")
    optional_fields = ("maker_fee_rate", "taker_fee_rate", "funding_rate", "long_carry_rate", "short_carry_rate")
    values = {name: _decimal(data.get(name)) for name in decimal_fields}
    values.update({name: _decimal(data[name]) if data.get(name) is not None else None for name in optional_fields})
    return Instrument(
        venue=data["venue"], symbol=data["symbol"], base_asset=data.get("base_asset", ""),
        quote_asset=data.get("quote_asset", ""), settlement_asset=data.get("settlement_asset", ""),
        next_funding_at=_dt(data.get("next_funding_at")), trading_enabled=bool(data.get("trading_enabled", True)),
        observed_at=_dt(data.get("observed_at")) or utc_now(), raw=data.get("raw", {}), **values,
    )


def ticker(data: dict[str, Any]) -> Ticker:
    return Ticker(
        data["venue"], data["symbol"], _decimal(data["bid"]), _decimal(data["ask"]),
        _decimal(data.get("bid_quantity")), _decimal(data.get("ask_quantity")),
        _dt(data.get("exchange_time")), _dt(data.get("received_at")) or utc_now(),
    )


def order_book(data: dict[str, Any]) -> OrderBookSnapshot:
    levels = lambda rows: tuple((_decimal(row[0]), _decimal(row[1])) for row in rows)
    return OrderBookSnapshot(
        data["venue"], data["symbol"], levels(data.get("bids", [])), levels(data.get("asks", [])),
        data.get("sequence"), _dt(data.get("exchange_time")), _dt(data.get("received_at")) or utc_now(),
        bool(data.get("synchronized", True)),
    )


def order(data: dict[str, Any]) -> OrderSnapshot:
    return OrderSnapshot(
        venue=data["venue"], symbol=data["symbol"], client_order_id=data.get("client_order_id", ""),
        venue_order_id=str(data.get("venue_order_id", "")), status=OrderStatus(data["status"]),
        side=Side(data["side"]), order_type=OrderType(data["order_type"]),
        requested_quantity=_decimal(data["requested_quantity"]), filled_quantity=_decimal(data.get("filled_quantity")),
        remaining_quantity=_decimal(data.get("remaining_quantity")),
        average_price=_decimal(data["average_price"]) if data.get("average_price") is not None else None,
        price=_decimal(data["price"]) if data.get("price") is not None else None,
        commission=_decimal(data.get("commission")), position_side=PositionSide(data.get("position_side", "NET")),
        updated_at=_dt(data.get("updated_at")) or utc_now(), raw=data.get("raw", {}),
    )


def order_request(data: dict[str, Any]) -> OrderRequest:
    return OrderRequest(
        venue=data["venue"], symbol=data["symbol"], side=Side(data["side"]), quantity=_decimal(data["quantity"]),
        client_order_id=data["client_order_id"], order_type=OrderType(data.get("order_type", "market")),
        price=_decimal(data["price"]) if data.get("price") is not None else None,
        time_in_force=TimeInForce(data.get("time_in_force", "GTC")), post_only=bool(data.get("post_only", False)),
        reduce_only=bool(data.get("reduce_only", False)), position_side=PositionSide(data.get("position_side", "NET")),
        metadata=data.get("metadata", {}),
    )


def fill(data: dict[str, Any]) -> Fill:
    return Fill(
        venue=data["venue"], symbol=data["symbol"], trade_id=str(data["trade_id"]),
        client_order_id=data.get("client_order_id", ""), venue_order_id=str(data.get("venue_order_id", "")),
        side=Side(data["side"]), quantity=_decimal(data["quantity"]), price=_decimal(data["price"]),
        commission=_decimal(data.get("commission")), commission_asset=data.get("commission_asset", ""),
        is_maker=data.get("is_maker"), occurred_at=_dt(data.get("occurred_at")) or utc_now(), raw=data.get("raw", {}),
    )


def credential_check(data: dict[str, Any]) -> CredentialCheck:
    return CredentialCheck(
        data["venue"], data["environment"], str(data.get("account_id", "")), bool(data["valid"]),
        bool(data["can_read"]), bool(data["can_trade"]),
        tuple(CredentialCheckItem(**item) for item in data.get("items", [])),
        _dt(data.get("checked_at")) or utc_now(),
    )


def event(data: dict[str, Any]) -> VenueEvent:
    return VenueEvent(
        event_id=data["event_id"], venue=data["venue"], event_type=VenueEventType(data["event_type"]),
        occurred_at=_dt(data.get("occurred_at")) or utc_now(),
        received_at=_dt(data.get("received_at")) or utc_now(),
        order=order(data["order"]) if data.get("order") else None,
        fill=fill(data["fill"]) if data.get("fill") else None,
        account=account(data["account"]) if data.get("account") else None,
        position=position(data["position"]) if data.get("position") else None,
        reconciliation=bool(data.get("reconciliation", False)), raw=data.get("raw", {}),
    )
