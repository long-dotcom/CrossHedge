"""Hyperliquid 原生账户、行情和执行 Connector。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.config.settings import HYPERLIQUID_MAINNET_API_URL
from app.core.http_client import post_hyperliquid_info
from app.core.time_utils import utc_now
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
from app.venues.hyperliquid.websocket import HyperliquidWebSocketRuntime, _order_status
from app.venues.protocols import EventHandler


class HyperliquidConnector:
    venue = "hyperliquid"
    capabilities = VenueCapabilities(
        venue="hyperliquid",
        supports_post_only=True,
        supports_reduce_only=True,
        supports_market_websocket=True,
        supports_private_websocket=True,
        supports_native_order_events=True,
    )

    def __init__(
        self,
        *,
        credentials: dict[str, Any] | None = None,
        environment: str = "live",
        read_only: bool = True,
        info_url: str | None = None,
        ws_url: str | None = None,
        default_min_notional: Decimal = Decimal("10"),
        default_maker_fee_rate: Decimal = Decimal("0.00015"),
        default_taker_fee_rate: Decimal = Decimal("0.00045"),
        slippage: float = 0.01,
        info_transport=None,
        exchange_factory=None,
    ) -> None:
        values = credentials or {}
        self.environment = str(environment or "live")
        self.read_only = bool(read_only)
        self.account_address = str(values.get("account_address") or "")
        self.secret_key = str(values.get("secret_key") or "")
        default_api = "https://api.hyperliquid-testnet.xyz" if self.environment == "testnet" else HYPERLIQUID_MAINNET_API_URL
        self.info_url = info_url or f"{default_api}/info"
        self.ws_url = ws_url or f"{default_api.replace('https://', 'wss://')}/ws"
        self.default_min_notional = default_min_notional
        self.default_maker_fee_rate = default_maker_fee_rate
        self.default_taker_fee_rate = default_taker_fee_rate
        self.slippage = float(slippage)
        self._info_transport = info_transport or post_hyperliquid_info
        self._exchange_factory = exchange_factory
        self._exchange = None
        self._instruments: dict[str, Instrument] = {}
        self._ws = HyperliquidWebSocketRuntime(
            ws_url=self.ws_url,
            account_address=self.account_address,
        )

    def start(self) -> None:
        self._ws.start()

    def stop(self) -> None:
        self._ws.stop()

    def health(self) -> dict:
        try:
            self._info({"type": "meta"})
            return {"venue": self.venue, "status": "ok", **self._ws.health()}
        except Exception as exc:
            return {"venue": self.venue, "status": "degraded", "error": str(exc), **self._ws.health()}

    def get_account(self) -> AccountSnapshot:
        self._require_account()
        perp = self._info({"type": "clearinghouseState", "user": self.account_address})
        spot = self._info({"type": "spotClearinghouseState", "user": self.account_address})
        margin = perp.get("marginSummary") or perp.get("crossMarginSummary") or {}
        balances = tuple(
            Balance(
                asset=str(item.get("coin") or ""),
                wallet_balance=_decimal(item.get("total")),
                available_balance=max(_decimal(item.get("total")) - _decimal(item.get("hold")), Decimal("0")),
                locked_balance=_decimal(item.get("hold")),
            )
            for item in spot.get("balances", [])
            if _decimal(item.get("total")) != 0
        )
        return AccountSnapshot(
            venue=self.venue,
            account_id=self.account_address,
            currency="USDC",
            equity=_decimal(margin.get("accountValue")),
            available_balance=_decimal(perp.get("withdrawable")),
            margin_used=_decimal(margin.get("totalMarginUsed")),
            balances=balances,
            raw={"perp": perp, "spot": spot},
        )

    def get_positions(self) -> list[Position]:
        self._require_account()
        data = self._info({"type": "clearinghouseState", "user": self.account_address})
        mids = self._info({"type": "allMids"})
        rows = []
        for wrapper in data.get("assetPositions", []):
            item = wrapper.get("position") or wrapper
            signed_quantity = _decimal(item.get("szi"))
            if signed_quantity == 0:
                continue
            symbol = str(item.get("coin") or "")
            rows.append(
                Position(
                    venue=self.venue,
                    account_id=self.account_address,
                    symbol=symbol,
                    position_side=PositionSide.NET,
                    quantity=abs(signed_quantity),
                    entry_price=_decimal(item.get("entryPx")),
                    mark_price=_decimal(mids.get(symbol) or item.get("entryPx")),
                    unrealized_pnl=_decimal(item.get("unrealizedPnl")),
                    margin_used=_decimal(item.get("marginUsed")),
                    liquidation_price=_optional_decimal(item.get("liquidationPx")),
                    external_position_id=symbol,
                    raw=item,
                )
            )
        return rows

    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]:
        self._require_account()
        rows = self._info({"type": "openOrders", "user": self.account_address})
        return [
            self._order_from_open(item)
            for item in rows if isinstance(item, dict) and (not symbol or str(item.get("coin")) == symbol)
        ]

    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]:
        requested = set(symbols) if symbols else None
        meta, contexts = self._info({"type": "metaAndAssetCtxs"})
        fees = self._user_fees()
        rows = []
        for item, context in zip(meta.get("universe", []), contexts):
            symbol = str(item.get("name") or "")
            if requested is not None and symbol not in requested:
                continue
            decimals = max(int(item.get("szDecimals", 0) or 0), 0)
            instrument = Instrument(
                venue=self.venue,
                symbol=symbol,
                base_asset=symbol.split(":", 1)[-1],
                quote_asset="USDC",
                settlement_asset="USDC",
                quantity_step=Decimal(1).scaleb(-decimals),
                minimum_quantity=Decimal(1).scaleb(-decimals),
                price_tick=Decimal("0"),
                minimum_notional=self.default_min_notional,
                maker_fee_rate=fees[0],
                taker_fee_rate=fees[1],
                funding_rate=_optional_decimal(context.get("funding")),
                trading_enabled=not bool(item.get("isDelisted", False)),
                raw={"meta": item, "context": context},
            )
            self._instruments[symbol] = instrument
            rows.append(instrument)
        return rows

    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument:
        if not refresh and symbol in self._instruments:
            return self._instruments[symbol]
        rows = self.get_instruments([symbol])
        if not rows:
            raise LookupError(f"Hyperliquid 品种不存在: {symbol}")
        return rows[0]

    def get_ticker(self, symbol: str) -> Ticker:
        cached = self._ws.ticker(symbol)
        if cached:
            return cached
        book = self.get_order_book(symbol, 1)
        if not book.bids or not book.asks:
            raise RuntimeError(f"Hyperliquid 订单簿为空: {symbol}")
        return Ticker(
            self.venue,
            symbol,
            book.bids[0][0],
            book.asks[0][0],
            book.bids[0][1],
            book.asks[0][1],
            exchange_time=book.exchange_time,
        )

    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        cached = self._ws.order_book(symbol, depth)
        if cached:
            return cached
        data = self._info({"type": "l2Book", "coin": symbol})
        levels = data.get("levels") or [[], []]
        return OrderBookSnapshot(
            self.venue,
            symbol,
            tuple((_decimal(item.get("px")), _decimal(item.get("sz"))) for item in levels[0][:depth]),
            tuple((_decimal(item.get("px")), _decimal(item.get("sz"))) for item in levels[1][:depth]),
            exchange_time=_millis_datetime(data.get("time")),
        )

    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None:
        self._ws.add_symbols(symbols)
        if handler:
            self._ws.add_event_handler(handler)

    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None:
        self._ws.remove_symbols(symbols)

    def subscribe_private_events(self, handler: EventHandler) -> None:
        self._require_account()
        self._ws.add_event_handler(handler, private=True)

    def submit_order(self, request: OrderRequest) -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("Hyperliquid Connector 为只读配置，禁止下单")
        exchange = self._get_exchange()
        cloid = self._cloid(request.client_order_id)
        self._ws.register_client_order_id(cloid.to_raw(), request.client_order_id)
        is_buy = request.side == Side.BUY
        if request.order_type == OrderType.LIMIT:
            if request.price is None:
                raise ValueError("Hyperliquid 限价单缺少价格")
            result = exchange.order(
                request.symbol,
                is_buy,
                float(request.quantity),
                float(request.price),
                {"limit": {"tif": "Alo" if request.post_only else request.time_in_force.value.title()}},
                reduce_only=request.reduce_only,
                cloid=cloid,
            )
        elif request.reduce_only:
            result = exchange.market_close(
                request.symbol,
                float(request.quantity),
                slippage=self.slippage,
                cloid=cloid,
            )
        else:
            result = exchange.market_open(
                request.symbol,
                is_buy,
                float(request.quantity),
                slippage=self.slippage,
                cloid=cloid,
            )
        return self._order_from_submit(result, request)

    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        if self.read_only:
            raise PermissionError("Hyperliquid Connector 为只读配置，禁止撤单")
        exchange = self._get_exchange()
        if venue_order_id:
            exchange.cancel(symbol, int(venue_order_id))
        elif client_order_id:
            exchange.cancel_by_cloid(symbol, self._cloid(client_order_id))
        else:
            raise ValueError("撤单必须提供订单 ID")
        current = self.get_order(symbol, client_order_id=client_order_id, venue_order_id=venue_order_id)
        if current.status == OrderStatus.ACCEPTED:
            return OrderSnapshot(**{**current.__dict__, "status": OrderStatus.PENDING_CANCEL})
        return current

    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot:
        self._require_account()
        identity: Any = int(venue_order_id) if venue_order_id else self._cloid(client_order_id).to_raw()
        data = self._info({"type": "orderStatus", "user": self.account_address, "oid": identity})
        if str(data.get("status") or "") != "order":
            raise LookupError(f"Hyperliquid 订单不存在: {venue_order_id or client_order_id}")
        order = data.get("order") or {}
        raw_order = order.get("order") or order
        status = _order_status(order.get("status"))
        return self._order_from_open(raw_order, status=status, client_order_id=client_order_id)

    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]:
        self._require_account()
        rows = self._info({"type": "userFills", "user": self.account_address})
        result = []
        for item in rows:
            if symbol and str(item.get("coin")) != symbol:
                continue
            if venue_order_id and str(item.get("oid")) != venue_order_id:
                continue
            result.append(
                Fill(
                    venue=self.venue,
                    symbol=str(item.get("coin") or ""),
                    trade_id=str(item.get("tid") or ""),
                    client_order_id=client_order_id,
                    venue_order_id=str(item.get("oid") or ""),
                    side=Side.BUY if str(item.get("side") or "B") == "B" else Side.SELL,
                    quantity=_decimal(item.get("sz")),
                    price=_decimal(item.get("px")),
                    commission=_decimal(item.get("fee")),
                    commission_asset=str(item.get("feeToken") or "USDC"),
                    is_maker=not bool(item.get("crossed")),
                    occurred_at=_millis_datetime(item.get("time")) or utc_now(),
                    raw=item,
                )
            )
        return result

    def validate_credentials(self) -> CredentialCheck:
        items: list[CredentialCheckItem] = []
        if not self.account_address:
            items.append(CredentialCheckItem("account_address", False, "缺少账户地址"))
            return CredentialCheck(self.venue, self.environment, "", False, False, False, tuple(items))
        can_read = False
        try:
            self._info({"type": "clearinghouseState", "user": self.account_address})
            can_read = True
            items.append(CredentialCheckItem("account", True, "账户状态可读"))
        except Exception as exc:
            items.append(CredentialCheckItem("account", False, f"账户读取失败: {exc}"))
        key_valid = False
        signer_address = ""
        if self.secret_key:
            try:
                import eth_account

                signer_address = eth_account.Account.from_key(self.secret_key).address
                key_valid = True
                items.append(
                    CredentialCheckItem(
                        "signer",
                        True,
                        f"签名私钥格式有效，signer={signer_address}",
                        blocking=False,
                    )
                )
            except Exception as exc:
                items.append(CredentialCheckItem("signer", False, f"签名私钥无效: {exc}", blocking=not self.read_only))
        else:
            items.append(CredentialCheckItem("signer", False, "未配置签名私钥", blocking=not self.read_only))
        can_trade = key_valid and not self.read_only
        valid = all(item.ok or not item.blocking for item in items)
        return CredentialCheck(self.venue, self.environment, self.account_address, valid, can_read, can_trade, tuple(items))

    def _info(self, payload: dict[str, Any]):
        return self._info_transport(self.info_url, payload)

    def _require_account(self) -> None:
        if not self.account_address:
            raise RuntimeError("Hyperliquid 账户地址未配置")

    def _user_fees(self) -> tuple[Decimal, Decimal]:
        if not self.account_address:
            return self.default_maker_fee_rate, self.default_taker_fee_rate
        try:
            data = self._info({"type": "userFees", "user": self.account_address})
            return (
                _decimal(data.get("userAddRate") or self.default_maker_fee_rate),
                _decimal(data.get("userCrossRate") or self.default_taker_fee_rate),
            )
        except Exception:
            return self.default_maker_fee_rate, self.default_taker_fee_rate

    def _get_exchange(self):
        if self._exchange is not None:
            return self._exchange
        if not self.secret_key or not self.account_address:
            raise RuntimeError("Hyperliquid 实盘下单需要账户地址和签名私钥")
        if self._exchange_factory:
            self._exchange = self._exchange_factory()
            return self._exchange
        import eth_account
        from hyperliquid.exchange import Exchange

        wallet = eth_account.Account.from_key(self.secret_key)
        self._exchange = Exchange(
            wallet,
            base_url=self.info_url.removesuffix("/info"),
            account_address=self.account_address,
        )
        return self._exchange

    @staticmethod
    def _cloid(client_order_id: str):
        from hyperliquid.utils.types import Cloid

        raw = hashlib.sha256(client_order_id.encode()).hexdigest()[:32]
        return Cloid.from_str(f"0x{raw}")

    def _order_from_submit(self, result: dict[str, Any], request: OrderRequest) -> OrderSnapshot:
        if str(result.get("status") or "") != "ok":
            raise RuntimeError(f"Hyperliquid 下单失败: {result}")
        statuses = (((result.get("response") or {}).get("data") or {}).get("statuses") or [])
        if not statuses:
            return self._submit_snapshot(request, "", OrderStatus.UNKNOWN)
        first = statuses[0]
        if "filled" in first:
            item = first["filled"]
            filled = _decimal(item.get("totalSz") or request.quantity)
            return self._submit_snapshot(
                request,
                str(item.get("oid") or ""),
                OrderStatus.FILLED,
                filled=filled,
                average=_optional_decimal(item.get("avgPx")),
            )
        if "resting" in first:
            item = first["resting"]
            return self._submit_snapshot(request, str(item.get("oid") or ""), OrderStatus.ACCEPTED)
        raise RuntimeError(f"Hyperliquid 下单被拒绝: {first.get('error') or first}")

    def _submit_snapshot(
        self,
        request: OrderRequest,
        venue_order_id: str,
        status: OrderStatus,
        *,
        filled: Decimal = Decimal("0"),
        average: Decimal | None = None,
    ) -> OrderSnapshot:
        return OrderSnapshot(
            venue=self.venue,
            symbol=request.symbol,
            client_order_id=request.client_order_id,
            venue_order_id=venue_order_id,
            status=status,
            side=request.side,
            order_type=request.order_type,
            requested_quantity=request.quantity,
            filled_quantity=filled,
            remaining_quantity=max(request.quantity - filled, Decimal("0")),
            average_price=average,
            price=request.price,
            position_side=PositionSide.NET,
            raw={},
        )

    def _order_from_open(
        self,
        item: dict[str, Any],
        *,
        status: OrderStatus = OrderStatus.ACCEPTED,
        client_order_id: str = "",
    ) -> OrderSnapshot:
        requested = _decimal(item.get("origSz") or item.get("sz"))
        remaining = _decimal(item.get("sz"))
        cloid = str(item.get("cloid") or "").lower()
        resolved_client_id = client_order_id or self._ws._cloid_to_client.get(cloid, cloid)
        return OrderSnapshot(
            venue=self.venue,
            symbol=str(item.get("coin") or ""),
            client_order_id=resolved_client_id,
            venue_order_id=str(item.get("oid") or ""),
            status=status,
            side=Side.BUY if str(item.get("side") or "B") == "B" else Side.SELL,
            order_type=OrderType.LIMIT,
            requested_quantity=requested,
            filled_quantity=max(requested - remaining, Decimal("0")),
            remaining_quantity=remaining,
            price=_optional_decimal(item.get("limitPx")),
            raw=item,
        )


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _optional_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    return _decimal(value)


def _millis_datetime(value: Any):
    from datetime import datetime, timezone

    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)
