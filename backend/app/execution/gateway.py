"""
执行网关模块
============

将适配器（Adapter）的下单接口封装为统一的执行网关，提供：
- ``LegOrderIntent`` —— 单腿下单意图
- ``GatewayOrderResult`` —— 网关下单结果（含订单事件和成交事件）
- ``AdapterExecutionGateway`` —— 基于适配器的网关实现

使用方式::

    from app.execution.gateway import LegOrderIntent, build_execution_gateway

    gateway = build_execution_gateway(adapter)
    result = gateway.submit_order(LegOrderIntent(...))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.adapters.base import AdapterOrder, AdapterOrderResult, ExchangeAdapter
from app.core.time_utils import utc_now


@dataclass(frozen=True)
class LegOrderIntent:
    """单腿下单意图。"""
    platform: str
    symbol: str
    side: str
    quantity: float
    venue_symbol: str | None = None
    order_type: str = "market"
    price: float | None = None
    post_only: bool = False
    reduce_only: bool = False
    ttl_seconds: int = 0
    hedge_group_id: int | None = None
    client_order_id: str = ""


@dataclass(frozen=True)
class ExecutionIntent:
    """对冲组执行意图（多腿）。"""
    hedge_group_id: int
    symbol: str
    mode: str
    legs: tuple[LegOrderIntent, ...]


@dataclass(frozen=True)
class OrderEvent:
    """订单事件。"""
    platform: str
    symbol: str
    side: str
    status: str
    external_order_id: str
    requested_quantity: float
    filled_quantity: float
    average_price: float
    fee: float
    message: str = ""
    occurred_at: object | None = None


@dataclass(frozen=True)
class FillEvent:
    """成交事件。"""
    platform: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    external_order_id: str
    occurred_at: object | None = None


@dataclass(frozen=True)
class PositionEvent:
    """仓位事件。"""
    platform: str
    symbol: str
    quantity: float
    average_price: float
    source: str
    occurred_at: object | None = None


@dataclass(frozen=True)
class HedgeGroupState:
    """对冲组状态快照。"""
    hedge_group_id: int
    status: str
    order_events: tuple[OrderEvent, ...] = ()
    fill_events: tuple[FillEvent, ...] = ()


@dataclass(frozen=True)
class GatewayOrderResult:
    """网关下单结果。"""
    success: bool
    order_event: OrderEvent
    fill_events: tuple[FillEvent, ...]
    adapter_result: AdapterOrderResult


class ExecutionGateway(Protocol):
    """执行网关协议。"""

    def submit_order(self, intent: LegOrderIntent, *, paper_latency_ms: int = 0) -> GatewayOrderResult:
        ...

    def cancel_order(self, platform: str, external_order_id: str) -> bool:
        ...

    def query_order(self, platform: str, external_order_id: str) -> dict:
        ...

    def reconcile(self, hedge_group_id: int) -> HedgeGroupState:
        ...


class AdapterExecutionGateway:
    """基于适配器的执行网关实现。"""

    def __init__(self, adapter: ExchangeAdapter) -> None:
        self.adapter = adapter

    def submit_order(self, intent: LegOrderIntent, *, paper_latency_ms: int = 0) -> GatewayOrderResult:
        """提交订单，返回网关结果。"""
        result = self.adapter.place_order(
            AdapterOrder(
                platform=intent.platform,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                venue_symbol=intent.venue_symbol,
                price=intent.price,
                order_type=intent.order_type,
                post_only=intent.post_only,
                reduce_only=intent.reduce_only,
                ttl_seconds=intent.ttl_seconds,
                paper_latency_ms=paper_latency_ms,
            )
        )
        occurred_at = utc_now()
        order_event = OrderEvent(
            platform=intent.platform,
            symbol=intent.symbol,
            side=intent.side,
            status=result.status,
            external_order_id=result.external_order_id,
            requested_quantity=intent.quantity,
            filled_quantity=result.filled_quantity,
            average_price=result.average_price,
            fee=result.fee,
            message=result.error_message,
            occurred_at=occurred_at,
        )
        fill_events = ()
        if result.success and result.filled_quantity > 0:
            fill_events = (
                FillEvent(
                    platform=intent.platform,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=result.filled_quantity,
                    price=result.average_price,
                    fee=result.fee,
                    external_order_id=result.external_order_id,
                    occurred_at=occurred_at,
                ),
            )
        return GatewayOrderResult(result.success, order_event, fill_events, result)

    def cancel_order(self, platform: str, external_order_id: str) -> bool:
        """撤销订单。"""
        return self.adapter.cancel_order(external_order_id)

    def query_order(self, platform: str, external_order_id: str) -> dict:
        """查询订单状态。"""
        return self.adapter.get_order(external_order_id)

    def reconcile(self, hedge_group_id: int) -> HedgeGroupState:
        """对冲组对账（默认实现）。"""
        return HedgeGroupState(hedge_group_id=hedge_group_id, status="unknown")


def build_execution_gateway(adapter: ExchangeAdapter) -> ExecutionGateway:
    """根据适配器构建执行网关。"""
    return AdapterExecutionGateway(adapter)
