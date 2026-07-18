"""原生连接器测试替身的统一订单快照工厂。"""

from decimal import Decimal

from app.venues.domain.models import OrderSnapshot, OrderStatus, OrderType, Side


def order_snapshot(
    request=None,
    *,
    venue: str = "binance",
    symbol: str = "XAUUSDT",
    status: str = "filled",
    venue_order_id: str = "venue-order",
    requested: float | None = None,
    filled: float = 0.0,
    price: float = 0.0,
    commission: float = 0.0,
) -> OrderSnapshot:
    quantity = Decimal(str(requested if requested is not None else getattr(request, "quantity", filled)))
    filled_quantity = Decimal(str(filled))
    return OrderSnapshot(
        venue=venue,
        symbol=str(getattr(request, "symbol", symbol)),
        client_order_id=str(getattr(request, "client_order_id", "")),
        venue_order_id=venue_order_id,
        status=OrderStatus(status.upper()),
        side=getattr(request, "side", Side.BUY),
        order_type=getattr(request, "order_type", OrderType.MARKET),
        requested_quantity=quantity,
        filled_quantity=filled_quantity,
        remaining_quantity=max(quantity - filled_quantity, Decimal("0")),
        average_price=Decimal(str(price)) if price else None,
        commission=Decimal(str(commission)),
    )
