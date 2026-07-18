"""交易场所能力声明。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class VenueCapabilities:
    venue: str
    supports_market_orders: bool = True
    supports_limit_orders: bool = True
    supports_post_only: bool = False
    supports_reduce_only: bool = False
    supports_hedge_mode: bool = False
    supports_order_replace: bool = False
    supports_client_order_id: bool = True
    supports_market_websocket: bool = False
    supports_private_websocket: bool = False
    supports_incremental_order_book: bool = False
    supports_native_order_events: bool = False
    requires_position_side: bool = False
