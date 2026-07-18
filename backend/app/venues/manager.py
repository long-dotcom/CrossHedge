"""原生 Connector 生命周期和工厂管理。"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from threading import RLock
from typing import Any

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.models import ExchangeCredential, SymbolMapping
from app.db.session import SessionLocal
from app.exchanges.credentials import build_credential_connector
from app.market.orderbook import order_book_cache
from app.market.quotes import quote_cache
from app.venues.binance import BinanceFuturesConnector
from app.venues.domain.models import OrderBookSnapshot, Ticker
from app.venues.hyperliquid import HyperliquidConnector
from app.venues.mt5 import MT5Connector
from app.venues.paper import PaperConnector
from app.venues.protocols import EventHandler, VenueConnector

logger = get_logger(__name__)


class NativeVenueManager:
    """按场所、模式和凭据版本复用长生命周期 Connector。"""

    def __init__(self) -> None:
        self._connectors: dict[tuple[str, str, str], VenueConnector] = {}
        self._handlers: list[EventHandler] = []
        self._lock = RLock()

    def connector_for(self, venue: str, execution_mode: str = "live") -> VenueConnector:
        normalized = str(venue or "").strip().lower()
        mode = str(execution_mode or "live").strip().lower()
        if mode == "paper":
            key = (normalized, mode, "paper")
            return self._get_or_create(
                key,
                lambda: PaperConnector(
                    venue=normalized,
                    ticker_provider=lambda symbol: _paper_ticker(normalized, symbol),
                    book_provider=lambda symbol, depth: _paper_book(normalized, symbol, depth),
                ),
            )
        if mode != "live":
            raise ValueError(f"不支持的执行模式: {execution_mode}")

        with SessionLocal() as db:
            row = (
                db.query(ExchangeCredential)
                .filter(ExchangeCredential.venue == normalized, ExchangeCredential.enabled.is_(True))
                .one_or_none()
            )
            if row is not None:
                identity = f"{row.environment}:{row.credentials_fingerprint}:{int(row.read_only)}"
                key = (normalized, mode, identity)
                connector = self._existing(key)
                if connector is not None:
                    return connector
                created = build_credential_connector(row)
                return self._store(key, created)

        settings = get_settings()
        if normalized == "hyperliquid":
            identity = f"legacy:{settings.hyperliquid.account_address}:{settings.hyperliquid.info_url}"
            key = (normalized, mode, identity)
            return self._get_or_create(
                key,
                lambda: HyperliquidConnector(
                    credentials={
                        "account_address": settings.hyperliquid.account_address,
                        "secret_key": settings.hyperliquid.secret_key,
                    },
                    read_only=not bool(settings.hyperliquid.secret_key),
                    info_url=settings.hyperliquid.info_url,
                    ws_url=settings.hyperliquid.ws_url,
                    default_min_notional=Decimal(str(settings.hyperliquid.default_min_notional)),
                    default_maker_fee_rate=Decimal(str(settings.hyperliquid.default_maker_fee_rate)),
                    default_taker_fee_rate=Decimal(str(settings.hyperliquid.default_taker_fee_rate)),
                    slippage=settings.hyperliquid.paper_live_slippage,
                ),
            )
        if normalized == "mt5":
            identity = f"legacy:{settings.mt5.login}:{settings.mt5.server}"
            key = (normalized, mode, identity)
            return self._get_or_create(
                key,
                lambda: MT5Connector(
                    credentials={
                        "login": settings.mt5.login,
                        "password": settings.mt5.password,
                        "server": settings.mt5.server,
                    },
                    read_only=not bool(settings.mt5.live_order_enabled or settings.mt5.demo_order_enabled),
                    environment="demo" if settings.mt5.demo_order_enabled and not settings.mt5.live_order_enabled else "live",
                    order_deviation_points=settings.mt5.order_deviation_points,
                    order_magic=settings.mt5.order_magic,
                    poll_interval_ms=settings.mt5.order_poll_interval_ms,
                ),
            )
        if normalized == "binance":
            raise RuntimeError("Binance 必须在交易所设置中配置并启用原生凭据")
        raise ValueError(f"尚未接入原生交易场所: {normalized}")

    def preload_enabled(self, *, start: bool = True, subscribe_market_data: bool = True) -> list[str]:
        with SessionLocal() as db:
            venues = [
                venue for (venue,) in db.query(ExchangeCredential.venue)
                .filter(ExchangeCredential.enabled.is_(True))
                .all()
                if venue in {"hyperliquid", "mt5", "binance"}
            ]
        for venue in ("hyperliquid", "mt5"):
            if venue not in venues:
                venues.append(venue)
        loaded = []
        for venue in venues:
            connector = self.connector_for(venue, "live")
            if start:
                connector.start()
            if subscribe_market_data:
                symbols = self.configured_symbols(venue)
                if symbols:
                    connector.subscribe_market_data(symbols)
            loaded.append(venue)
        return loaded

    def configured_symbols(self, venue: str) -> tuple[str, ...]:
        normalized = str(venue).lower()
        with SessionLocal() as db:
            rows = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
        values: list[str] = []
        for row in rows:
            for index in ("a", "b"):
                row_venue = str(getattr(row, f"leg_{index}_venue", "") or "").lower()
                symbol = str(getattr(row, f"leg_{index}_symbol", "") or "")
                if not symbol:
                    symbol = str(
                        getattr(row, "leg_a_venue_symbol", "")
                        if index == "a"
                        else getattr(row, "mt5_symbol", "")
                    )
                if row_venue == normalized and symbol and symbol not in values:
                    values.append(symbol)
        return tuple(values)

    def add_event_handler(self, handler: EventHandler) -> None:
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)
            connectors = tuple(self._connectors.values())
        for connector in connectors:
            self._subscribe_private(connector, handler)

    def health_snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            connectors = tuple(self._connectors.items())
        rows = []
        for (venue, mode, identity), connector in connectors:
            payload = connector.health()
            payload.update({
                "venue": venue,
                "mode": mode,
                "identity": identity,
                "environment": str(getattr(connector, "environment", "live")),
                "read_only": bool(getattr(connector, "read_only", False)),
            })
            rows.append(payload)
        return rows

    def invalidate(self, venue: str | None = None) -> None:
        normalized = str(venue or "").strip().lower()
        with self._lock:
            keys = [key for key in self._connectors if not normalized or key[0] == normalized]
            connectors = [self._connectors.pop(key) for key in keys]
        for connector in connectors:
            connector.stop()

    def reset(self) -> None:
        self.invalidate()

    def _existing(self, key: tuple[str, str, str]) -> VenueConnector | None:
        with self._lock:
            return self._connectors.get(key)

    def _get_or_create(self, key: tuple[str, str, str], factory: Callable[[], VenueConnector]) -> VenueConnector:
        existing = self._existing(key)
        return existing if existing is not None else self._store(key, factory())

    def _store(self, key: tuple[str, str, str], connector: VenueConnector) -> VenueConnector:
        with self._lock:
            existing = self._connectors.get(key)
            if existing is not None:
                connector.stop()
                return existing
            self._connectors[key] = connector
            handlers = tuple(self._handlers)
        for handler in handlers:
            self._subscribe_private(connector, handler)
        return connector

    @staticmethod
    def _subscribe_private(connector: VenueConnector, handler: EventHandler) -> None:
        try:
            connector.subscribe_private_events(handler)
        except (PermissionError, RuntimeError) as exc:
            logger.warning("连接器未启用私有事件订阅: venue={}, error={}", getattr(connector, "venue", ""), exc)


def _paper_ticker(venue: str, symbol: str) -> Ticker:
    quote = quote_cache.latest(venue, symbol)
    if quote is None:
        raise LookupError(f"Paper 行情缓存不存在: {venue}:{symbol}")
    return Ticker(
        venue,
        symbol,
        Decimal(str(quote.bid)),
        Decimal(str(quote.ask)),
        exchange_time=quote.exchange_ts,
        received_at=quote.local_recv_ts,
    )


def _paper_book(venue: str, symbol: str, depth: int) -> OrderBookSnapshot:
    book = order_book_cache.latest(venue, symbol)
    if book is None:
        ticker = _paper_ticker(venue, symbol)
        return OrderBookSnapshot(
            venue,
            symbol,
            ((ticker.bid, Decimal("100000") / ticker.bid),),
            ((ticker.ask, Decimal("100000") / ticker.ask),),
        )
    return OrderBookSnapshot(
        venue,
        symbol,
        tuple((Decimal(str(level.price)), Decimal(str(level.size))) for level in book.bids[:depth]),
        tuple((Decimal(str(level.price)), Decimal(str(level.size))) for level in book.asks[:depth]),
        exchange_time=book.exchange_ts,
        received_at=book.local_recv_ts,
    )


native_venue_manager = NativeVenueManager()
