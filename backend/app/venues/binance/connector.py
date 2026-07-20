"""Binance USDⓈ-M Futures 原生 Connector。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.time_utils import utc_now
from app.core.cache import TTLCache
from app.venues.binance.rest import BinanceApiError, BinanceFuturesRestClient, normalize_symbol
from app.venues.binance.websocket import BinanceWebSocketRuntime
from app.venues.domain.capabilities import VenueCapabilities
from app.venues.domain.models import (
    AccountSnapshot,
    Balance,
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
from app.venues.protocols import EventHandler


class BinanceFuturesConnector:
    venue = "binance"
    capabilities = VenueCapabilities(
        venue="binance",
        supports_post_only=True,
        supports_reduce_only=True,
        supports_hedge_mode=True,
        supports_market_websocket=True,
        supports_private_websocket=True,
        supports_incremental_order_book=True,
        supports_native_order_events=True,
        requires_position_side=True,
    )

    def __init__(
        self,
        *,
        credentials: dict[str, Any] | None = None,
        environment: str = "live",
        read_only: bool = True,
        rest_client: BinanceFuturesRestClient | None = None,
        default_maker_fee_rate: Decimal = Decimal("0.0002"),
        default_taker_fee_rate: Decimal = Decimal("0.0005"),
    ) -> None:
        values = credentials or {}
        self.environment = environment
        self.read_only = bool(read_only)
        self.default_maker_fee_rate = default_maker_fee_rate
        self.default_taker_fee_rate = default_taker_fee_rate
        self.rest = rest_client or BinanceFuturesRestClient(
            api_key=str(values.get("api_key") or ""),
            api_secret=str(values.get("api_secret") or ""),
            environment=environment,
        )
        self._has_credentials = bool(
            str(getattr(self.rest, "api_key", "") or "")
            and str(getattr(self.rest, "api_secret", "") or "")
        )
        self._instrument_cache: TTLCache[Instrument] = TTLCache(
            ttl_seconds=21600, namespace=f"binance-instruments-{environment}",
        )
        self._event_handlers: list[EventHandler] = []
        self._ticker_cache: TTLCache[Ticker] = TTLCache(
            ttl_seconds=10, namespace=f"binance-tickers-{environment}",
        )
        self._ws = BinanceWebSocketRuntime(
            self.rest,
            on_ticker=self._cache_ticker,
        )

    def start(self) -> None:
        self.rest.synchronize_clock()

    def stop(self) -> None:
        self._ws.stop()

    def health(self) -> dict:
        """健康检查只读取本地 WS 状态，禁止周期性消耗 REST 配额。"""
        websocket = self._ws.health()
        connected = bool(websocket.get("market_ws_connected"))
        if not self.read_only:
            connected = bool(websocket.get("private_ws_connected"))
        return {
            "venue": self.venue,
            "status": "ok" if connected else "degraded",
            "error": "" if connected else "Binance WebSocket 未连接",
            **websocket,
        }

    def get_account(self) -> AccountSnapshot:
        self._require_private_access()
        cached = self._ws.account()
        if cached is not None:
            return cached
        data = self.rest.account()
        balances = tuple(
            Balance(
                asset=str(item.get("asset") or ""),
                wallet_balance=_decimal(item.get("walletBalance")),
                available_balance=_decimal(item.get("availableBalance")),
                locked_balance=max(
                    _decimal(item.get("walletBalance")) - _decimal(item.get("availableBalance")), Decimal("0")
                ),
            )
            for item in data.get("assets", [])
            if _decimal(item.get("walletBalance")) != 0
        )
        snapshot = AccountSnapshot(
            venue=self.venue,
            account_id=str(data.get("accountAlias") or "binance-futures"),
            currency="USDT",
            equity=_decimal(data.get("totalMarginBalance") or data.get("totalWalletBalance")),
            available_balance=_decimal(data.get("availableBalance")),
            margin_used=_decimal(data.get("totalInitialMargin")),
            maintenance_margin=_decimal(data.get("totalMaintMargin")),
            balances=balances,
            raw=data,
        )
        self._ws.seed_account(snapshot)
        return snapshot

    def get_positions(self) -> list[Position]:
        self._require_private_access()
        cached = self._ws.positions()
        if cached is not None:
            return cached
        rows = []
        for item in self.rest.position_risk():
            quantity = _decimal(item.get("positionAmt"))
            if quantity == 0:
                continue
            raw_side = str(item.get("positionSide") or "BOTH").upper()
            position_side = PositionSide.NET if raw_side == "BOTH" else PositionSide(raw_side)
            rows.append(
                Position(
                    venue=self.venue,
                    account_id="binance-futures",
                    symbol=str(item.get("symbol") or ""),
                    position_side=position_side,
                    quantity=abs(quantity),
                    entry_price=_decimal(item.get("entryPrice")),
                    mark_price=_decimal(item.get("markPrice")),
                    unrealized_pnl=_decimal(item.get("unRealizedProfit")),
                    margin_used=_decimal(item.get("initialMargin")),
                    liquidation_price=_optional_decimal(item.get("liquidationPrice")),
                    external_position_id=f"{item.get('symbol', '')}-{raw_side}",
                    raw=item,
                )
            )
        self._ws.seed_positions(rows)
        return rows

    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]:
        self._require_private_access()
        return [self._order_from_payload(item) for item in self.rest.open_orders(symbol)]

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]:
        requested = {normalize_symbol(symbol) for symbol in symbols} if symbols else None
        info = self.rest.exchange_info()
        rows = []
        for item in info.get("symbols", []):
            symbol = str(item.get("symbol") or "")
            if requested is not None and symbol not in requested:
                continue
            instrument = self._instrument_from_payload(item)
            self._instrument_cache.set(symbol, instrument)
            rows.append(instrument)
        return rows

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        key = normalize_symbol(symbol)
        cached = self._instrument_cache.get(key) if not refresh else None
        if cached is not None:
            return cached
        rows = self.get_instruments([key])
        if not rows:
            raise LookupError(f"Binance Futures 品种不存在: {key}")
        instrument = rows[0]
        maker_fee_rate = self.default_maker_fee_rate
        taker_fee_rate = self.default_taker_fee_rate
        if self._has_credentials:
            try:
                fee = self.rest.commission_rate(key)
                maker_fee_rate = _decimal(fee.get("makerCommissionRate"))
                taker_fee_rate = _decimal(fee.get("takerCommissionRate"))
            except BinanceApiError:
                pass
        funding = self.rest.premium_index(key)
        instrument = Instrument(
            **{
                **instrument.__dict__,
                "maker_fee_rate": maker_fee_rate,
                "taker_fee_rate": taker_fee_rate,
                "funding_rate": _optional_decimal(funding.get("lastFundingRate")),
                "next_funding_at": _millis_datetime(funding.get("nextFundingTime")),
            }
        )
        self._instrument_cache.set(key, instrument)
        return instrument

    def get_ticker(self, symbol: str) -> Ticker:
        cached = self._ticker_cache.get(normalize_symbol(symbol))
        if cached is not None:
            return cached
        data = self.rest.book_ticker(symbol)
        return Ticker(
            self.venue,
            normalize_symbol(symbol),
            _decimal(data.get("bidPrice")),
            _decimal(data.get("askPrice")),
            _decimal(data.get("bidQty")),
            _decimal(data.get("askQty")),
            exchange_time=_millis_datetime(data.get("time")),
        )

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        cached = self._ws.order_book(symbol, depth)
        if cached is not None and cached.synchronized:
            return cached
        limit = next((value for value in (5, 10, 20, 50, 100, 500, 1000) if value >= depth), 1000)
        data = self.rest.depth(symbol, limit)
        return OrderBookSnapshot(
            self.venue,
            normalize_symbol(symbol),
            tuple((_decimal(price), _decimal(quantity)) for price, quantity in data.get("bids", [])[:depth]),
            tuple((_decimal(price), _decimal(quantity)) for price, quantity in data.get("asks", [])[:depth]),
            sequence=int(data.get("lastUpdateId")) if data.get("lastUpdateId") is not None else None,
            exchange_time=_millis_datetime(data.get("E") or data.get("T")),
        )

    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None:
        self._ws.add_symbols(symbols)
        if handler is not None:
            self._ws.add_event_handler(handler)

    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None:
        self._ws.remove_symbols(symbols)

    def subscribe_private_events(self, handler: EventHandler) -> None:
        self._require_private_access()
        if handler not in self._event_handlers:
            self._event_handlers.append(handler)
        self._ws.add_event_handler(handler)

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("Binance Connector 为只读配置，禁止下单")
        self._require_private_access()
        if not self._ws.private_stream_ready:
            raise RuntimeError("Binance 账户私有 WebSocket 尚未连接，禁止提交无法实时确认的订单")
        symbol = normalize_symbol(request.symbol)
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": request.side.value.upper(),
            "type": request.order_type.value.upper(),
            "quantity": decimal_text(request.quantity),
            "newClientOrderId": request.client_order_id,
            "newOrderRespType": "RESULT",
        }
        if request.position_side != PositionSide.NET:
            params["positionSide"] = request.position_side.value
        elif request.reduce_only:
            params["reduceOnly"] = True
        if request.order_type == OrderType.LIMIT:
            if request.price is None:
                raise ValueError("Binance 限价单缺少价格")
            params["price"] = decimal_text(request.price)
            params["timeInForce"] = "GTX" if request.post_only else request.time_in_force.value
        return self._order_from_payload(self.rest.place_order(params), fallback=request)

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("Binance Connector 为只读配置，禁止撤单")
        self._require_private_access()
        data = self.rest.cancel_order(
            symbol,
            client_order_id=client_order_id,
            order_id=venue_order_id,
        )
        return self._order_from_payload(data)

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        self._require_private_access()
        return self._order_from_payload(
            self.rest.query_order(symbol, client_order_id=client_order_id, order_id=venue_order_id)
        )

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        self._require_private_access()
        if not symbol:
            raise ValueError("Binance 成交查询必须提供 symbol")
        rows = self.rest.user_trades(symbol, order_id=venue_order_id)
        return [
            Fill(
                venue=self.venue,
                symbol=str(item.get("symbol") or normalize_symbol(symbol)),
                trade_id=str(item.get("id") or ""),
                client_order_id=client_order_id,
                venue_order_id=str(item.get("orderId") or venue_order_id),
                side=Side.BUY if bool(item.get("buyer")) else Side.SELL,
                quantity=_decimal(item.get("qty")),
                price=_decimal(item.get("price")),
                commission=_decimal(item.get("commission")),
                commission_asset=str(item.get("commissionAsset") or ""),
                is_maker=bool(item.get("maker")),
                occurred_at=_millis_datetime(item.get("time")) or utc_now(),
                raw=item,
            )
            for item in rows
        ]

    def validate_credentials(self) -> CredentialCheck:
        items: list[CredentialCheckItem] = []
        account_id = ""
        can_read = False
        can_trade = False
        try:
            offset = self.rest.synchronize_clock()
            items.append(CredentialCheckItem("clock", abs(offset) < 5000, f"服务器时间偏差 {offset}ms"))
        except Exception as exc:
            items.append(CredentialCheckItem("connectivity", False, f"公共 API 连接失败: {exc}"))
            return CredentialCheck(self.venue, self.environment, account_id, False, False, False, tuple(items))
        try:
            account = self.rest.account()
            account_id = str(account.get("accountAlias") or "binance-futures")
            can_read = True
            api_can_trade = bool(account.get("canTrade"))
            can_trade = api_can_trade and not self.read_only
            items.append(CredentialCheckItem("account", True, "USDⓈ-M Futures 账户可读"))
            items.append(
                CredentialCheckItem(
                    "trade_permission",
                    api_can_trade,
                    "API 具有 Futures 交易权限" if api_can_trade else "API 没有 Futures 交易权限",
                    blocking=not self.read_only,
                )
            )
            mode = self.rest.position_mode()
            hedge_mode = bool(mode.get("dualSidePosition"))
            items.append(
                CredentialCheckItem(
                    "position_mode",
                    hedge_mode,
                    "账户为 Hedge Mode" if hedge_mode else "账户为 One-way Mode",
                    blocking=False,
                )
            )
        except Exception as exc:
            items.append(CredentialCheckItem("account", False, f"账户验证失败: {exc}"))
        valid = all(item.ok or not item.blocking for item in items)
        return CredentialCheck(self.venue, self.environment, account_id, valid, can_read, can_trade, tuple(items))

    def _cache_ticker(self, ticker: Ticker) -> None:
        self._ticker_cache.set(ticker.symbol, ticker)

    def _require_private_access(self) -> None:
        if not self._has_credentials:
            raise PermissionError("Binance 私有账户功能需要配置 API 凭据")

    def _instrument_from_payload(self, item: dict[str, Any]) -> Instrument:
        filters = {str(value.get("filterType")): value for value in item.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") or filters.get("LOT_SIZE") or {}
        price_filter = filters.get("PRICE_FILTER") or {}
        notional_filter = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
        return Instrument(
            venue=self.venue,
            symbol=str(item.get("symbol") or ""),
            base_asset=str(item.get("baseAsset") or ""),
            quote_asset=str(item.get("quoteAsset") or ""),
            settlement_asset=str(item.get("marginAsset") or item.get("quoteAsset") or ""),
            quantity_step=_decimal(lot.get("stepSize")),
            minimum_quantity=_decimal(lot.get("minQty")),
            price_tick=_decimal(price_filter.get("tickSize")),
            minimum_notional=_decimal(notional_filter.get("notional") or notional_filter.get("minNotional")),
            trading_enabled=str(item.get("status") or "") == "TRADING",
            maker_fee_rate=self.default_maker_fee_rate,
            taker_fee_rate=self.default_taker_fee_rate,
            raw=item,
        )

    def _order_from_payload(self, data: dict[str, Any], fallback: OrderRequest | None = None) -> OrderSnapshot:
        status = _order_status(data.get("status"))
        requested = _decimal(data.get("origQty") or (fallback.quantity if fallback else 0))
        filled = _decimal(data.get("executedQty"))
        average = _optional_decimal(data.get("avgPrice"))
        side_value = str(data.get("side") or (fallback.side.value if fallback else "buy")).lower()
        type_value = str(data.get("type") or (fallback.order_type.value if fallback else "market")).lower()
        raw_position_side = str(data.get("positionSide") or (fallback.position_side.value if fallback else "BOTH"))
        return OrderSnapshot(
            venue=self.venue,
            symbol=str(data.get("symbol") or (normalize_symbol(fallback.symbol) if fallback else "")),
            client_order_id=str(data.get("clientOrderId") or (fallback.client_order_id if fallback else "")),
            venue_order_id=str(data.get("orderId") or ""),
            status=status,
            side=Side(side_value),
            order_type=OrderType.LIMIT if type_value == "limit" else OrderType.MARKET,
            requested_quantity=requested,
            filled_quantity=filled,
            remaining_quantity=max(requested - filled, Decimal("0")),
            average_price=average if average and average > 0 else None,
            price=_optional_decimal(data.get("price")) or (fallback.price if fallback else None),
            position_side=PositionSide.NET if raw_position_side == "BOTH" else PositionSide(raw_position_side),
            error_message=str(data.get("msg") or data.get("rejectReason") or ""),
            updated_at=_millis_datetime(data.get("updateTime")) or utc_now(),
            raw=data,
        )


def _order_status(value: Any) -> OrderStatus:
    normalized = str(value or "NEW").upper()
    return {
        "NEW": OrderStatus.ACCEPTED,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "CANCELLED": OrderStatus.CANCELED,
        "EXPIRED": OrderStatus.EXPIRED,
        "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
        "REJECTED": OrderStatus.REJECTED,
    }.get(normalized, OrderStatus.UNKNOWN)


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _millis_datetime(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)
