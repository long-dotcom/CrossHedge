"""
Paper 模拟适配器
================

提供纯内存的模拟交易所适配器，用于开发调试和策略回测。
当没有连接真实交易所时，Paper 适配器会根据本地报价缓存和订单簿
模拟成交，支持 Maker / Taker 两种下单模式。

主要功能：
- 模拟行情生成（基于确定性随机种子）
- 模拟 Maker 挂单（Post-Only）成交逻辑
- 模拟 Taker 吃单成交（使用 L2 订单簿深度）
- 模拟手续费计算（对接 Hyperliquid 费率）
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import Callable

from app.adapters.base import Account, AdapterOrder, AdapterOrderResult, Ticker
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.quotes import quote_cache
from app.strategy.live_costs import HyperliquidCostInputs, hyperliquid_cost_inputs

logger = get_logger(__name__)


class PaperAdapter:
    """Paper 模拟适配器基类。

    提供完整的模拟交易逻辑，HyperliquidAdapter 和 MT5Adapter 均继承此类，
    在非实盘模式下使用 Paper 模拟成交。

    参数:
        platform: 平台标识（如 ``"hyperliquid"`` / ``"mt5"``）。
        price_bias_bps: 价格偏移（基点），用于模拟买卖价差偏差。
        fee_rate_provider: 手续费率提供函数，接受品种名称返回费率信息。
    """

    def __init__(
        self,
        platform: str,
        price_bias_bps: float = 0.0,
        fee_rate_provider: Callable[[str], HyperliquidCostInputs] | None = None,
    ) -> None:
        self.platform = platform
        self.price_bias_bps = price_bias_bps
        self._fee_rate_provider = fee_rate_provider or hyperliquid_cost_inputs
        # 已提交的模拟订单记录
        self._orders: dict[str, AdapterOrderResult] = {}

    def get_symbols(self) -> list[str]:
        """返回默认支持的品种列表。"""
        return ["BTC", "ETH", "SOL"]

    def get_account(self) -> Account:
        """返回模拟账户摘要（固定值）。"""
        return Account(
            platform=self.platform,
            equity=50_000.0,
            available_balance=35_000.0,
            margin_used=5_000.0,
            margin_ratio=0.82,
        )

    def get_positions(self) -> list[dict]:
        """Paper 模式下无真实持仓，返回空列表。"""
        return []

    def get_ticker(self, symbol: str) -> Ticker:
        """生成模拟行情快照。

        使用确定性随机种子（基于平台 + 品种 + 当前分钟），
        保证同一分钟内行情一致，便于调试和测试。
        """
        # 基础价格映射
        base = {"BTC": 65000.0, "ETH": 3400.0, "SOL": 145.0}.get(
            symbol.upper().replace("USD", ""), 100.0
        )
        # 使用当前分钟的种子保证确定性
        seed = int(
            hashlib.sha256(
                f"{self.platform}:{symbol}:{utc_now().minute}".encode()
            ).hexdigest()[:8],
            16,
        )
        rng = random.Random(seed)
        drift_bps = rng.uniform(-10, 10) + self.price_bias_bps
        mid = base * (1 + drift_bps / 10_000)
        spread = mid * (4 + rng.uniform(0, 4)) / 10_000
        return Ticker(
            symbol=symbol,
            bid=round(mid - spread / 2, 4),
            ask=round(mid + spread / 2, 4),
            depth_notional=100_000.0,
            timestamp=utc_now(),
        )

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """生成模拟订单簿（单档深度）。"""
        ticker = self.get_ticker(symbol)
        return {
            "bids": [[ticker.bid, ticker.depth_notional / depth]],
            "asks": [[ticker.ask, ticker.depth_notional / depth]],
        }

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        """模拟下单。

        处理逻辑：
        1. 如果设置了模拟延迟，先 sleep
        2. 获取执行用行情（优先使用缓存报价，否则使用模拟行情）
        3. Maker 挂单（Post-Only）：检查是否会吃单，TTL 足够时成交
        4. Taker 吃单：Hyperliquid 平台使用 L2 订单簿模拟成交
        5. 计算手续费并返回成交结果
        """
        # 模拟延迟
        if order.paper_latency_ms > 0:
            time.sleep(order.paper_latency_ms / 1000)

        ticker = self._execution_ticker(order.symbol)

        # --- Maker 挂单（Post-Only）逻辑 ---
        if order.order_type == "limit" and order.post_only:
            marketable = (
                (order.side.lower() == "buy" and order.price is not None and order.price >= ticker.ask)
                or (order.side.lower() == "sell" and order.price is not None and order.price <= ticker.bid)
            )
            if marketable:
                return AdapterOrderResult(
                    False, "", "rejected", 0.0, 0.0, 0.0,
                    "post-only 价格会吃单",
                )
            # TTL 不足时不成交
            if order.ttl_seconds < 1:
                return AdapterOrderResult(
                    False, "", "unfilled", 0.0, 0.0, 0.0,
                    "maker 挂单超时未成交",
                )
            price = order.price or (
                ticker.bid if order.side.lower() == "buy" else ticker.ask
            )
        else:
            # --- Taker 吃单逻辑 ---
            if self.platform == "hyperliquid":
                book = order_book_cache.latest(self.platform, order.symbol)
                if book:
                    fill = simulate_market_fill(book, order.side, order.quantity)
                    if not fill.enough_liquidity:
                        return AdapterOrderResult(
                            False, "", "unfilled",
                            fill.filled_quantity, fill.average_price, 0.0,
                            f"L2 深度不足: 目标 {order.quantity:.8f}，可成交 {fill.filled_quantity:.8f}",
                        )
                    price = fill.average_price
                else:
                    price = ticker.ask if order.side.lower() == "buy" else ticker.bid
            else:
                price = ticker.ask if order.side.lower() == "buy" else ticker.bid

        # 计算手续费
        fee = abs(order.quantity * price) * self._fee_rate(order)
        external_id = f"paper-{self.platform}-{len(self._orders) + 1}"
        result = AdapterOrderResult(
            success=True,
            external_order_id=external_id,
            status="filled",
            filled_quantity=order.quantity,
            average_price=price,
            fee=fee,
        )
        self._orders[external_id] = result
        return result

    def _execution_ticker(self, symbol: str) -> Ticker:
        """获取执行用行情：优先使用缓存报价，否则使用模拟行情。"""
        quote = quote_cache.latest(self.platform, symbol)
        if quote:
            return Ticker(
                symbol=symbol,
                bid=quote.bid,
                ask=quote.ask,
                depth_notional=quote.depth_notional,
                timestamp=quote.local_recv_ts,
            )
        return self.get_ticker(symbol)

    def _fee_rate(self, order: AdapterOrder) -> float:
        """计算手续费率。

        非 Hyperliquid 平台不收取手续费。
        Hyperliquid 平台根据 Maker / Taker 类型返回对应费率。
        """
        if self.platform != "hyperliquid":
            return 0.0
        symbol = order.venue_symbol or order.symbol
        # 非 DEX 品种：直接使用全局默认费率
        if ":" not in symbol:
            settings = get_settings()
            return (
                settings.hyperliquid.default_maker_fee_rate
                if _is_maker_order(order)
                else settings.hyperliquid.default_taker_fee_rate
            )
        # DEX 品种：尝试从费率提供函数获取
        try:
            costs = self._fee_rate_provider(symbol)
            return costs.maker_fee_rate if _is_maker_order(order) else costs.taker_fee_rate
        except Exception:
            settings = get_settings()
            fallback = (
                settings.hyperliquid.default_maker_fee_rate
                if _is_maker_order(order)
                else settings.hyperliquid.default_taker_fee_rate
            )
            # 未知 DEX 品种使用 20% 折扣费率
            return fallback * 0.2 if symbol.startswith("xyz:") else fallback

    def cancel_order(self, order_id: str) -> bool:
        """撤销模拟订单（仅检查订单是否存在）。"""
        return order_id in self._orders

    def get_order(self, order_id: str) -> dict:
        """查询模拟订单状态。"""
        result = self._orders.get(order_id)
        return result.__dict__ if result else {"status": "not_found"}

    def get_trades(self, order_id: str) -> list[dict]:
        """查询模拟订单成交明细。"""
        result = self._orders.get(order_id)
        if not result:
            return []
        return [
            {
                "order_id": order_id,
                "quantity": result.filled_quantity,
                "price": result.average_price,
                "fee": result.fee,
            }
        ]


def _is_maker_order(order: AdapterOrder) -> bool:
    """判断是否为 Maker（Post-Only 限价）订单。"""
    return order.order_type == "limit" and bool(order.post_only)
