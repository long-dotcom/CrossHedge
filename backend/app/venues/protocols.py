"""原生 Connector 的窄接口协议。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from app.venues.domain.capabilities import VenueCapabilities
from app.venues.domain.events import VenueEvent
from app.venues.domain.models import (
    AccountSnapshot,
    CredentialCheck,
    Fill,
    Instrument,
    OrderBookSnapshot,
    OrderRequest,
    OrderSnapshot,
    Position,
    Ticker,
)

EventHandler = Callable[[VenueEvent], None]


class AccountProvider(Protocol):
    def get_account(self) -> AccountSnapshot: ...
    def get_positions(self) -> list[Position]: ...
    def get_open_orders(self, symbol: str | None = None) -> list[OrderSnapshot]: ...


class InstrumentProvider(Protocol):
    def get_instruments(self, symbols: Sequence[str] | None = None) -> list[Instrument]: ...
    def get_instrument(self, symbol: str, *, refresh: bool = False) -> Instrument: ...


class MarketDataProvider(Protocol):
    def get_ticker(self, symbol: str) -> Ticker: ...
    def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot: ...
    def subscribe_market_data(self, symbols: Sequence[str], handler: EventHandler | None = None) -> None: ...
    def unsubscribe_market_data(self, symbols: Sequence[str]) -> None: ...


class ExecutionProvider(Protocol):
    def submit_order(self, request: OrderRequest) -> OrderSnapshot: ...
    def cancel_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot: ...
    def get_order(self, symbol: str, *, client_order_id: str = "", venue_order_id: str = "") -> OrderSnapshot: ...
    def get_fills(self, symbol: str | None = None, *, client_order_id: str = "", venue_order_id: str = "") -> list[Fill]: ...
    def subscribe_private_events(self, handler: EventHandler) -> None: ...


class CredentialValidator(Protocol):
    def validate_credentials(self) -> CredentialCheck: ...


class VenueConnector(
    AccountProvider,
    InstrumentProvider,
    MarketDataProvider,
    ExecutionProvider,
    CredentialValidator,
    Protocol,
):
    venue: str
    capabilities: VenueCapabilities

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> dict: ...
