"""MetaTrader 5 原生 Connector。

MT5 Python API 没有交易事件回调，本 Connector 对活动订单执行短周期查询，
并按 deal ticket 去重产生统一 Fill 事件；账户和品种信息仍按需拉取。
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from mt5_gateway.mt5_bootstrap import ensure_mt5_connected
from app.core.time_utils import utc_now
from app.venues.domain.capabilities import VenueCapabilities
from app.venues.domain.models import (
    AccountSnapshot,
    CredentialCheck,
    CredentialCheckItem,
    Fill,
    Instrument,
    OrderBookSnapshot,
    OrderRequest,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Side,
    Ticker,
)
from mt5_gateway.poller import MT5OrderPoller
from app.venues.protocols import EventHandler


class MT5Connector:
    venue = "mt5"
    capabilities = VenueCapabilities(
        venue="mt5",
        supports_post_only=False,
        supports_reduce_only=True,
        supports_hedge_mode=True,
        supports_client_order_id=False,
        supports_market_websocket=False,
        supports_private_websocket=False,
        supports_native_order_events=False,
    )

    def __init__(
        self,
        *,
        credentials: dict[str, Any] | None = None,
        environment: str = "live",
        read_only: bool = True,
        mt5_module=None,
        connect=None,
        order_deviation_points: int = 20,
        order_magic: int = 260620,
        poll_interval_ms: int = 75,
    ) -> None:
        values = credentials or {}
        self.environment = str(environment or "live")
        self.read_only = bool(read_only)
        self.login = str(values.get("login") or "")
        self.password = str(values.get("password") or "")
        self.server = str(values.get("server") or "")
        self.order_deviation_points = int(order_deviation_points)
        self.order_magic = int(order_magic)
        self._mt5 = mt5_module
        self._connect = connect
        self._lock = threading.RLock()
        self._client_by_ticket: dict[str, str] = {}
        self._poller = MT5OrderPoller(self, interval_ms=poll_interval_ms)

    @property
    def mt5(self):
        if self._mt5 is None:
            import MetaTrader5 as mt5

            self._mt5 = mt5
        return self._mt5

    def start(self) -> None:
        self._ensure_connected()

    def stop(self) -> None:
        self._poller.stop()
        try:
            self.mt5.shutdown()
        except Exception:
            pass

    def health(self) -> dict:
        try:
            connected = self._ensure_connected()
            terminal = self.mt5.terminal_info() if connected else None
            return {
                "venue": self.venue,
                "status": "ok" if connected and terminal else "degraded",
                "connected": bool(connected and terminal),
                **self._poller.health(),
            }
        except Exception as exc:
            return {"venue": self.venue, "status": "degraded", "connected": False, "error": str(exc)}

    def get_account(self) -> AccountSnapshot:
        self._ensure_connected_or_raise()
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info 为空: {self.mt5.last_error()}")
        return AccountSnapshot(
            venue=self.venue,
            account_id=str(getattr(info, "login", self.login) or self.login),
            currency=str(getattr(info, "currency", "USD") or "USD"),
            equity=_decimal(getattr(info, "equity", 0)),
            available_balance=_decimal(getattr(info, "margin_free", 0)),
            margin_used=_decimal(getattr(info, "margin", 0)),
            maintenance_margin=Decimal("0"),
            raw=_asdict(info),
        )

    def get_positions(self) -> list[Position]:
        self._ensure_connected_or_raise()
        rows = []
        for item in self.mt5.positions_get() or []:
            quantity = _decimal(getattr(item, "volume", 0))
            if quantity <= 0:
                continue
            is_buy = int(getattr(item, "type", 0)) == int(getattr(self.mt5, "POSITION_TYPE_BUY", 0))
            rows.append(
                Position(
                    venue=self.venue,
                    account_id=self.login,
                    symbol=str(getattr(item, "symbol", "")),
                    position_side=PositionSide.LONG if is_buy else PositionSide.SHORT,
                    quantity=quantity,
                    entry_price=_decimal(getattr(item, "price_open", 0)),
                    mark_price=_decimal(getattr(item, "price_current", 0)),
                    unrealized_pnl=_decimal(getattr(item, "profit", 0)),
                    external_position_id=str(getattr(item, "ticket", "")),
                    raw=_asdict(item),
                )
            )
        return rows

    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]:
        self._ensure_connected_or_raise()
        values = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        return [self._order_from_mt5(item) for item in values or []]

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]:
        self._ensure_connected_or_raise()
        if symbols:
            values = [self.mt5.symbol_info(symbol) for symbol in symbols]
        else:
            values = self.mt5.symbols_get() or []
        return [self._instrument_from_info(item) for item in values if item is not None]

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        self._ensure_connected_or_raise()
        self.mt5.symbol_select(symbol, True)
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise LookupError(f"MT5 品种不存在: {symbol}")
        return self._instrument_from_info(info)

    def get_ticker(self, symbol: str) -> Ticker:
        self._ensure_connected_or_raise()
        self.mt5.symbol_select(symbol, True)
        tick = self.mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"MT5 tick 不可用: {symbol}")
        return Ticker(
            self.venue,
            symbol,
            _decimal(getattr(tick, "bid", 0)),
            _decimal(getattr(tick, "ask", 0)),
            _decimal(getattr(tick, "bid_volume_real", 0) or getattr(tick, "volume_real", 0)),
            _decimal(getattr(tick, "ask_volume_real", 0) or getattr(tick, "volume_real", 0)),
            exchange_time=_millis_datetime(getattr(tick, "time_msc", 0)),
        )

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        self._ensure_connected_or_raise()
        if not self.mt5.market_book_add(symbol):
            ticker = self.get_ticker(symbol)
            return OrderBookSnapshot(
                self.venue,
                symbol,
                ((ticker.bid, ticker.bid_quantity),),
                ((ticker.ask, ticker.ask_quantity),),
            )
        try:
            rows = self.mt5.market_book_get(symbol) or []
            bids = []
            asks = []
            buy_types = {
                int(getattr(self.mt5, "BOOK_TYPE_BUY", 2)),
                int(getattr(self.mt5, "BOOK_TYPE_BUY_MARKET", 4)),
            }
            for item in rows:
                level = (_decimal(getattr(item, "price", 0)), _decimal(getattr(item, "volume_real", 0) or getattr(item, "volume", 0)))
                if int(getattr(item, "type", -1)) in buy_types:
                    bids.append(level)
                else:
                    asks.append(level)
            return OrderBookSnapshot(
                self.venue,
                symbol,
                tuple(sorted(bids, reverse=True)[:depth]),
                tuple(sorted(asks)[:depth]),
            )
        finally:
            self.mt5.market_book_release(symbol)

    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None:
        for symbol in symbols:
            self.mt5.symbol_select(symbol, True)

    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None:
        return None

    def subscribe_private_events(self, handler: EventHandler) -> None:
        self._poller.add_handler(handler)

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("MT5 Connector 为只读配置，禁止下单")
        if request.post_only:
            raise ValueError("MT5 不支持原生 Post-only")
        self._ensure_connected_or_raise()
        mt5 = self.mt5
        symbol = request.symbol
        mt5.symbol_select(symbol, True)
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"MT5 tick 不可用: {symbol}")
        is_buy = request.side == Side.BUY
        payload: dict[str, Any] = {
            "symbol": symbol,
            "volume": float(request.quantity),
            "magic": self.order_magic,
            "comment": _mt5_comment(request.client_order_id),
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if request.order_type == OrderType.MARKET:
            payload.update(
                {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                    "price": float(getattr(tick, "ask" if is_buy else "bid")),
                    "deviation": self.order_deviation_points,
                }
            )
            if request.reduce_only:
                position = self._matching_position(symbol, request.side, request.quantity)
                if position is None:
                    raise RuntimeError("MT5 reduce-only 未找到匹配持仓")
                payload["position"] = int(getattr(position, "ticket"))
        else:
            if request.price is None:
                raise ValueError("MT5 限价单缺少价格")
            payload.update(
                {
                    "action": mt5.TRADE_ACTION_PENDING,
                    "type": mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT,
                    "price": float(request.price),
                }
            )
        result = self._send_with_filling_fallback(payload, symbol)
        retcode = int(getattr(result, "retcode", 0))
        done = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
        partial = int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010))
        placed = int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008))
        if retcode not in {done, partial, placed}:
            raise RuntimeError(f"MT5 下单失败 retcode={retcode}: {getattr(result, 'comment', '')}")
        venue_order_id = str(int(getattr(result, "order", 0) or getattr(result, "deal", 0)))
        self._client_by_ticket[venue_order_id] = request.client_order_id
        filled = _decimal(getattr(result, "volume", 0)) if retcode in {done, partial} else Decimal("0")
        status = OrderStatus.FILLED if retcode == done else OrderStatus.PARTIALLY_FILLED if retcode == partial else OrderStatus.ACCEPTED
        snapshot = OrderSnapshot(
            venue=self.venue,
            symbol=symbol,
            client_order_id=request.client_order_id,
            venue_order_id=venue_order_id,
            status=status,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.quantity,
            filled_quantity=filled,
            remaining_quantity=max(request.quantity - filled, Decimal("0")),
            average_price=_positive_decimal(getattr(result, "price", 0)),
            price=request.price,
            position_side=request.position_side,
            raw=_asdict(result),
        )
        self._poller.watch(symbol, venue_order_id, request.client_order_id)
        return snapshot

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("MT5 Connector 为只读配置，禁止撤单")
        if not venue_order_id:
            raise ValueError("MT5 撤单必须提供 venue_order_id")
        self._ensure_connected_or_raise()
        result = self.mt5.order_send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": int(venue_order_id)})
        if result is None or int(getattr(result, "retcode", 0)) != int(getattr(self.mt5, "TRADE_RETCODE_DONE", 10009)):
            raise RuntimeError(f"MT5 撤单失败: {getattr(result, 'comment', self.mt5.last_error())}")
        current = self.get_order(symbol, venue_order_id=venue_order_id, client_order_id=client_order_id)
        return OrderSnapshot(**{**current.__dict__, "status": OrderStatus.PENDING_CANCEL})

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if not venue_order_id:
            raise ValueError("MT5 查询订单必须提供 venue_order_id")
        self._ensure_connected_or_raise()
        ticket = int(venue_order_id)
        values = self.mt5.orders_get(ticket=ticket) or self.mt5.history_orders_get(ticket=ticket) or []
        if not values:
            deals = self.mt5.history_deals_get(ticket=ticket) or []
            if deals:
                return self._order_from_deals(symbol, venue_order_id, client_order_id, deals)
            raise LookupError(f"MT5 订单不存在: {venue_order_id}")
        return self._order_from_mt5(values[0], client_order_id=client_order_id)

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        if not venue_order_id:
            return []
        self._ensure_connected_or_raise()
        deals = self.mt5.history_deals_get(ticket=int(venue_order_id)) or []
        return [self._fill_from_deal(item, client_order_id) for item in deals if _decimal(getattr(item, "volume", 0)) > 0]

    def validate_credentials(self) -> CredentialCheck:
        items: list[CredentialCheckItem] = []
        try:
            connected = self._ensure_connected()
            items.append(CredentialCheckItem("terminal", connected, "MT5 终端已连接" if connected else "MT5 终端连接失败"))
        except Exception as exc:
            items.append(CredentialCheckItem("terminal", False, f"MT5 连接失败: {exc}"))
            return CredentialCheck(self.venue, self.environment, self.login, False, False, False, tuple(items))
        info = self.mt5.account_info()
        terminal = self.mt5.terminal_info()
        can_read = info is not None
        trade_allowed = bool(getattr(info, "trade_allowed", False)) and bool(getattr(terminal, "trade_allowed", False))
        items.append(CredentialCheckItem("account", can_read, "账户信息可读" if can_read else "账户信息不可读"))
        items.append(
            CredentialCheckItem(
                "trade_permission",
                trade_allowed,
                "终端和账户允许交易" if trade_allowed else "终端或账户禁止交易",
                blocking=not self.read_only,
            )
        )
        if info is not None and self.login:
            matched = str(getattr(info, "login", "")) == self.login
            items.append(CredentialCheckItem("login", matched, "登录账号匹配" if matched else "当前终端账号与配置不匹配"))
        valid = all(item.ok or not item.blocking for item in items)
        return CredentialCheck(
            self.venue,
            self.environment,
            str(getattr(info, "login", self.login) if info else self.login),
            valid,
            can_read,
            trade_allowed and not self.read_only,
            tuple(items),
        )

    def _ensure_connected(self) -> bool:
        with self._lock:
            if self._connect is not None:
                return bool(self._connect())
            return ensure_mt5_connected(
                login=int(self.login) if self.login else None,
                password=self.password or None,
                server=self.server or None,
            )

    def _ensure_connected_or_raise(self) -> None:
        if not self._ensure_connected():
            raise RuntimeError(f"MT5 连接失败: {self.mt5.last_error()}")

    def _instrument_from_info(self, info) -> Instrument:
        return Instrument(
            venue=self.venue,
            symbol=str(getattr(info, "name", "")),
            base_asset=str(getattr(info, "currency_base", "")),
            quote_asset=str(getattr(info, "currency_profit", "")),
            settlement_asset=str(getattr(info, "currency_margin", "")),
            quantity_step=_decimal(getattr(info, "volume_step", 0)),
            minimum_quantity=_decimal(getattr(info, "volume_min", 0)),
            price_tick=_decimal(getattr(info, "trade_tick_size", 0) or getattr(info, "point", 0)),
            contract_size=_decimal(getattr(info, "trade_contract_size", 1)),
            long_carry_rate=_optional_decimal(getattr(info, "swap_long", None)),
            short_carry_rate=_optional_decimal(getattr(info, "swap_short", None)),
            trading_enabled=int(getattr(info, "trade_mode", 0)) != int(getattr(self.mt5, "SYMBOL_TRADE_MODE_DISABLED", 0)),
            raw=_asdict(info),
        )

    def _send_with_filling_fallback(self, payload: dict[str, Any], symbol: str):
        info = self.mt5.symbol_info(symbol)
        preferred = int(getattr(info, "filling_mode", getattr(self.mt5, "ORDER_FILLING_IOC", 1)))
        modes = [preferred]
        for name in ("ORDER_FILLING_IOC", "ORDER_FILLING_FOK", "ORDER_FILLING_RETURN"):
            value = int(getattr(self.mt5, name, -1))
            if value >= 0 and value not in modes:
                modes.append(value)
        last = None
        for mode in modes:
            last = self.mt5.order_send({**payload, "type_filling": mode})
            if last is None:
                continue
            if int(getattr(last, "retcode", 0)) != int(getattr(self.mt5, "TRADE_RETCODE_INVALID_FILL", 10030)):
                return last
        if last is None:
            raise RuntimeError(f"MT5 order_send 无返回: {self.mt5.last_error()}")
        return last

    def _matching_position(self, symbol: str, close_side: Side, quantity: Decimal):
        expected_type = (
            int(getattr(self.mt5, "POSITION_TYPE_SELL", 1))
            if close_side == Side.BUY
            else int(getattr(self.mt5, "POSITION_TYPE_BUY", 0))
        )
        candidates = [
            item for item in self.mt5.positions_get(symbol=symbol) or []
            if int(getattr(item, "type", -1)) == expected_type
            and _decimal(getattr(item, "volume", 0)) >= quantity
        ]
        return min(candidates, key=lambda item: _decimal(getattr(item, "volume", 0))) if candidates else None

    def _order_from_mt5(self, item, *, client_order_id: str = "") -> OrderSnapshot:
        ticket = str(getattr(item, "ticket", ""))
        requested = _decimal(getattr(item, "volume_initial", 0))
        remaining = _decimal(getattr(item, "volume_current", 0))
        filled = max(requested - remaining, Decimal("0"))
        order_type_value = int(getattr(item, "type", 0))
        sell_types = {
            int(getattr(self.mt5, "ORDER_TYPE_SELL", 1)),
            int(getattr(self.mt5, "ORDER_TYPE_SELL_LIMIT", 3)),
            int(getattr(self.mt5, "ORDER_TYPE_SELL_STOP", 5)),
        }
        limit_types = {
            int(getattr(self.mt5, "ORDER_TYPE_BUY_LIMIT", 2)),
            int(getattr(self.mt5, "ORDER_TYPE_SELL_LIMIT", 3)),
        }
        return OrderSnapshot(
            venue=self.venue,
            symbol=str(getattr(item, "symbol", "")),
            client_order_id=client_order_id or self._client_by_ticket.get(ticket, ""),
            venue_order_id=ticket,
            status=_mt5_order_status(self.mt5, int(getattr(item, "state", -1)), filled, requested),
            side=Side.SELL if order_type_value in sell_types else Side.BUY,
            order_type=OrderType.LIMIT if order_type_value in limit_types else OrderType.MARKET,
            requested_quantity=requested,
            filled_quantity=filled,
            remaining_quantity=remaining,
            average_price=_positive_decimal(
                getattr(item, "price_current", 0) or getattr(item, "price_open", 0)
            ),
            price=_positive_decimal(getattr(item, "price_open", 0)),
            updated_at=_seconds_datetime(getattr(item, "time_done", 0) or getattr(item, "time_setup", 0)) or utc_now(),
            raw=_asdict(item),
        )

    def _order_from_deals(self, symbol: str, venue_order_id: str, client_order_id: str, deals) -> OrderSnapshot:
        fills = [self._fill_from_deal(item, client_order_id) for item in deals]
        quantity = sum((fill.quantity for fill in fills), Decimal("0"))
        notional = sum((fill.quantity * fill.price for fill in fills), Decimal("0"))
        side = fills[0].side if fills else Side.BUY
        return OrderSnapshot(
            venue=self.venue,
            symbol=symbol,
            client_order_id=client_order_id or self._client_by_ticket.get(venue_order_id, ""),
            venue_order_id=venue_order_id,
            status=OrderStatus.FILLED,
            side=side,
            order_type=OrderType.MARKET,
            requested_quantity=quantity,
            filled_quantity=quantity,
            remaining_quantity=Decimal("0"),
            average_price=notional / quantity if quantity else None,
        )

    def _fill_from_deal(self, item, client_order_id: str) -> Fill:
        deal_type = int(getattr(item, "type", 0))
        side = Side.BUY if deal_type == int(getattr(self.mt5, "DEAL_TYPE_BUY", 0)) else Side.SELL
        ticket = str(getattr(item, "order", ""))
        return Fill(
            venue=self.venue,
            symbol=str(getattr(item, "symbol", "")),
            trade_id=str(getattr(item, "ticket", "")),
            client_order_id=client_order_id or self._client_by_ticket.get(ticket, ""),
            venue_order_id=ticket,
            side=side,
            quantity=_decimal(getattr(item, "volume", 0)),
            price=_decimal(getattr(item, "price", 0)),
            commission=_decimal(getattr(item, "commission", 0)) + _decimal(getattr(item, "fee", 0)),
            commission_asset=str(getattr(item, "currency", "")),
            occurred_at=_millis_datetime(getattr(item, "time_msc", 0)) or utc_now(),
            raw=_asdict(item),
        )


def _mt5_order_status(mt5, state: int, filled: Decimal, requested: Decimal) -> OrderStatus:
    if requested > 0 and filled >= requested:
        return OrderStatus.FILLED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    mapping = {
        int(getattr(mt5, "ORDER_STATE_STARTED", 0)): OrderStatus.SUBMITTED,
        int(getattr(mt5, "ORDER_STATE_PLACED", 1)): OrderStatus.ACCEPTED,
        int(getattr(mt5, "ORDER_STATE_PARTIAL", 3)): OrderStatus.PARTIALLY_FILLED,
        int(getattr(mt5, "ORDER_STATE_FILLED", 4)): OrderStatus.FILLED,
        int(getattr(mt5, "ORDER_STATE_CANCELED", 2)): OrderStatus.CANCELED,
        int(getattr(mt5, "ORDER_STATE_REJECTED", 6)): OrderStatus.REJECTED,
        int(getattr(mt5, "ORDER_STATE_EXPIRED", 5)): OrderStatus.EXPIRED,
    }
    return mapping.get(state, OrderStatus.UNKNOWN)


def _mt5_comment(client_order_id: str) -> str:
    digest = hashlib.sha256(client_order_id.encode()).hexdigest()[:20]
    return f"CH-{digest}"


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed > 0 else None


def _asdict(value: Any) -> dict[str, Any]:
    method = getattr(value, "_asdict", None)
    if callable(method):
        return dict(method())
    return {}


def _millis_datetime(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)


def _seconds_datetime(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)
