"""
交易所适配器基础数据模型
========================

定义所有交易所适配器（Paper / Hyperliquid / MT5 / Nautilus）共用的数据结构：

- :class:`Ticker`        —— 实时行情快照（买一 / 卖一 / 深度名义值）
- :class:`Account`       —— 账户权益摘要（净值 / 可用余额 / 保证金）
- :class:`AdapterOrder`  —— 适配器层统一下单请求
- :class:`AdapterOrderResult` —— 适配器层统一下单结果
- :class:`ExchangeAdapter`  —— 适配器协议（Protocol），定义各交易所必须实现的接口
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


# ---------------------------------------------------------------------------
# 行情快照
# ---------------------------------------------------------------------------

@dataclass
class Ticker:
    """单个品种的实时行情快照。

    属性:
        symbol: 品种名称（如 ``"BTC"`` / ``"EURUSD"``）。
        bid: 买一价。
        ask: 卖一价。
        depth_notional: 可用深度名义值（USD），用于估算可成交量。
        timestamp: 行情接收时间（naive UTC）。
    """

    symbol: str
    bid: float
    ask: float
    depth_notional: float
    timestamp: datetime


# ---------------------------------------------------------------------------
# 账户摘要
# ---------------------------------------------------------------------------

@dataclass
class Account:
    """交易所账户权益摘要。

    属性:
        platform: 平台标识（如 ``"hyperliquid"`` / ``"mt5"`` / ``"binance"``）。
        equity: 账户净值。
        available_balance: 可用余额。
        margin_used: 已用保证金。
        margin_ratio: 保证金比率（equity / margin_used），无保证金时为 1.0。
        currency: 账户计价币种，默认 ``"USD"``。
    """

    platform: str
    equity: float
    available_balance: float
    margin_used: float
    margin_ratio: float
    currency: str = "USD"


# ---------------------------------------------------------------------------
# 下单请求 / 结果
# ---------------------------------------------------------------------------

@dataclass
class AdapterOrder:
    """适配器层统一下单请求。

    属性:
        platform: 目标平台标识。
        symbol: 内部品种名称。
        side: 买卖方向，``"buy"`` 或 ``"sell"``。
        quantity: 下单数量。
        venue_symbol: 交易所侧品种名称（可选，不填时使用 symbol）。
        price: 限价单价格（市价单时可为 None）。
        order_type: 订单类型，默认 ``"market"``。
        post_only: 是否为 Post-Only（Maker）单。
        reduce_only: 是否为只减仓单。
        ttl_seconds: 挂单有效期（秒），0 表示不限制。
        paper_latency_ms: Paper 模式下模拟延迟（毫秒）。
    """

    platform: str
    symbol: str
    side: str
    quantity: float
    venue_symbol: str | None = None
    price: float | None = None
    order_type: str = "market"
    post_only: bool = False
    reduce_only: bool = False
    ttl_seconds: int = 0
    paper_latency_ms: int = 0


@dataclass
class AdapterOrderResult:
    """适配器层统一下单结果。

    属性:
        success: 下单是否成功。
        external_order_id: 交易所侧订单 ID。
        status: 订单状态（``"filled"`` / ``"rejected"`` / ``"unfilled"`` 等）。
        filled_quantity: 成交数量。
        average_price: 成交均价。
        fee: 手续费。
        error_message: 错误信息（成功时为空字符串）。
    """

    success: bool
    external_order_id: str
    status: str
    filled_quantity: float
    average_price: float
    fee: float
    error_message: str = ""


# ---------------------------------------------------------------------------
# 适配器协议
# ---------------------------------------------------------------------------

class ExchangeAdapter(Protocol):
    """交易所适配器协议（Protocol），定义各适配器必须实现的接口。

    所有适配器（Paper / Hyperliquid / MT5 / Nautilus）均需实现以下方法，
    以便上层策略和执行模块以统一方式调用不同交易所。
    """

    platform: str

    def get_symbols(self) -> list[str]:
        """返回该适配器支持的品种列表。"""
        ...

    def get_account(self) -> Account:
        """返回账户权益摘要。"""
        ...

    def get_positions(self) -> list[dict]:
        """返回当前持仓列表。"""
        ...

    def get_ticker(self, symbol: str) -> Ticker:
        """获取指定品种的实时行情快照。"""
        ...

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """获取指定品种的订单簿（L2 深度）。"""
        ...

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        """提交下单请求并返回结果。"""
        ...

    def cancel_order(self, order_id: str) -> bool:
        """撤销指定订单，返回是否成功。"""
        ...

    def get_order(self, order_id: str) -> dict:
        """查询指定订单的状态。"""
        ...

    def get_trades(self, order_id: str) -> list[dict]:
        """查询指定订单的成交明细。"""
        ...
