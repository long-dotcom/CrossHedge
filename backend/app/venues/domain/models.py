"""交易场所无关的数据模型。

本模块只表达 CrossHedge 需要的业务事实，不包含任何交易所 SDK 类型。
数量统一使用交易所下单单位；策略数量与 MT5 手数的转换在执行计划阶段完成。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.core.time_utils import utc_now


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class PositionSide(StrEnum):
    NET = "NET"
    LONG = "LONG"
    SHORT = "SHORT"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTX = "GTX"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    PENDING_CANCEL = "PENDING_CANCEL"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Balance:
    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    locked_balance: Decimal = Decimal("0")


@dataclass(frozen=True)
class AccountSnapshot:
    venue: str
    account_id: str
    currency: str
    equity: Decimal
    available_balance: Decimal
    margin_used: Decimal
    maintenance_margin: Decimal = Decimal("0")
    balances: tuple[Balance, ...] = ()
    observed_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Position:
    venue: str
    account_id: str
    symbol: str
    position_side: PositionSide
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    margin_used: Decimal = Decimal("0")
    liquidation_price: Decimal | None = None
    external_position_id: str = ""
    observed_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Instrument:
    venue: str
    symbol: str
    base_asset: str
    quote_asset: str
    settlement_asset: str
    quantity_step: Decimal
    minimum_quantity: Decimal
    price_tick: Decimal
    minimum_notional: Decimal = Decimal("0")
    contract_size: Decimal = Decimal("1")
    maker_fee_rate: Decimal | None = None
    taker_fee_rate: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_at: datetime | None = None
    long_carry_rate: Decimal | None = None
    short_carry_rate: Decimal | None = None
    trading_enabled: bool = True
    observed_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Ticker:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_quantity: Decimal = Decimal("0")
    ask_quantity: Decimal = Decimal("0")
    exchange_time: datetime | None = None
    received_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class OrderBookSnapshot:
    venue: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    sequence: int | None = None
    exchange_time: datetime | None = None
    received_at: datetime = field(default_factory=utc_now)
    synchronized: bool = True


@dataclass(frozen=True)
class OrderRequest:
    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    client_order_id: str
    order_type: OrderType = OrderType.MARKET
    price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    position_side: PositionSide = PositionSide.NET
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class OrderSnapshot:
    venue: str
    symbol: str
    client_order_id: str
    venue_order_id: str
    status: OrderStatus
    side: Side
    order_type: OrderType
    requested_quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0")
    average_price: Decimal | None = None
    price: Decimal | None = None
    commission: Decimal = Decimal("0")
    position_side: PositionSide = PositionSide.NET
    updated_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class Fill:
    venue: str
    symbol: str
    trade_id: str
    client_order_id: str
    venue_order_id: str
    side: Side
    quantity: Decimal
    price: Decimal
    commission: Decimal = Decimal("0")
    commission_asset: str = ""
    is_maker: bool | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class CredentialCheckItem:
    name: str
    ok: bool
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class CredentialCheck:
    venue: str
    environment: str
    account_id: str
    valid: bool
    can_read: bool
    can_trade: bool
    items: tuple[CredentialCheckItem, ...]
    checked_at: datetime = field(default_factory=utc_now)
