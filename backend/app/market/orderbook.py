"""
订单簿缓存与模拟成交模块
==========================

提供跨交易所订单簿的存储和模拟成交计算：

- :class:`BookLevel` —— 订单簿单档（价格 + 数量）
- :class:`OrderBook` —— 完整订单簿（买卖两侧多档深度）
- :class:`SimulatedFill` —— 模拟成交结果
- :class:`OrderBookCache` —— 线程安全的订单簿缓存
- :func:`simulate_market_fill` —— 按订单簿深度模拟市价成交
- :func:`parse_l2_levels` —— 解析 L2 深度数据（支持 dict 和 tuple 格式）

订单簿缓存按 ``(platform, symbol)`` 键存储最新一档订单簿快照。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.logging import get_logger
from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BookLevel:
    """订单簿单档数据。

    属性:
        price: 价格。
        size: 该档位的数量。
    """

    price: float
    size: float


@dataclass(frozen=True)
class OrderBook:
    """完整订单簿快照。

    属性:
        platform: 平台标识。
        symbol: 品种名称。
        bids: 买单档位（按价格降序）。
        asks: 卖单档位（按价格升序）。
        source: 数据来源标识。
        local_recv_ts: 本地接收时间（naive UTC）。
        exchange_ts: 交易所时间戳（可选）。
    """

    platform: str
    symbol: str
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    source: str
    local_recv_ts: object
    exchange_ts: object | None = None


@dataclass(frozen=True)
class SimulatedFill:
    """模拟成交结果。

    属性:
        requested_quantity: 请求成交数量。
        filled_quantity: 实际成交数量。
        average_price: 成交均价。
        notional: 成交名义值（数量 × 价格）。
        worst_price: 最差成交价。
        slippage_bps: 滑点（基点）。
        enough_liquidity: 流动性是否充足（完全成交）。
    """

    requested_quantity: float
    filled_quantity: float
    average_price: float
    notional: float
    worst_price: float
    slippage_bps: float
    enough_liquidity: bool


# ---------------------------------------------------------------------------
# 订单簿缓存
# ---------------------------------------------------------------------------

class OrderBookCache:
    """基于 Redis 的共享订单簿缓存。

    按 ``(platform, symbol)`` 键存储最新订单簿快照。
    每次写入会过滤无效档位（价格 ≤ 0 或数量 ≤ 0）并截断到最大档数。
    """

    def __init__(self, max_levels: int = 20, *, namespace: str | None = None) -> None:
        self.max_levels = max_levels
        # 固定命名空间用于跨容器共享；独立实例默认随机命名以避免互相污染。
        self._namespace = namespace or uuid.uuid4().hex

    def _key(self, platform: str, symbol: str) -> str:
        return redis_key("orderbooks", self._namespace, platform, symbol)

    def put(
        self,
        platform: str,
        symbol: str,
        bids: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        asks: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        source: str,
        exchange_ts=None,
    ) -> OrderBook:
        """写入订单簿快照。

        参数:
            platform: 平台标识。
            symbol: 品种名称。
            bids: 买单档位列表 ``[(price, size), ...]``。
            asks: 卖单档位列表 ``[(price, size), ...]``。
            source: 数据来源。
            exchange_ts: 交易所时间戳（可选）。

        返回:
            创建的 OrderBook 实例。
        """
        book = OrderBook(
            platform=platform,
            symbol=symbol,
            bids=tuple(
                BookLevel(float(price), float(size))
                for price, size in bids[: self.max_levels]
                if float(price) > 0 and float(size) > 0
            ),
            asks=tuple(
                BookLevel(float(price), float(size))
                for price, size in asks[: self.max_levels]
                if float(price) > 0 and float(size) > 0
            ),
            source=source,
            local_recv_ts=utc_now(),
            exchange_ts=exchange_ts,
        )
        redis_client().set(self._key(platform, symbol), _book_json(book))
        return book

    def latest(self, platform: str, symbol: str) -> OrderBook | None:
        """获取指定平台和品种的最新订单簿。"""
        raw = redis_client().get(self._key(platform, symbol))
        return _book_from_json(raw) if raw else None


# ---------------------------------------------------------------------------
# 模拟成交
# ---------------------------------------------------------------------------

def simulate_market_fill(book: OrderBook, side: str, quantity: float) -> SimulatedFill:
    """按订单簿深度模拟市价成交。

    从最优档位开始逐档吃单，直到满足请求数量或耗尽所有档位。

    参数:
        book: 订单簿快照。
        side: 买卖方向（``"buy"`` 吃 asks，``"sell"`` 吃 bids）。
        quantity: 请求成交数量。

    返回:
        模拟成交结果，包含成交量、均价、滑点等信息。
    """
    requested = max(float(quantity or 0.0), 0.0)
    if requested <= 0:
        return SimulatedFill(requested, 0.0, 0.0, 0.0, 0.0, 0.0, False)

    levels = book.asks if side.lower() == "buy" else book.bids
    reference_price = levels[0].price if levels else 0.0
    remaining = requested
    filled = 0.0
    notional = 0.0
    worst_price = 0.0

    for level in levels:
        take = min(remaining, level.size)
        if take <= 0:
            continue
        filled += take
        notional += take * level.price
        worst_price = level.price
        remaining -= take
        if remaining <= 1e-12:
            break

    average = notional / filled if filled > 0 else 0.0
    # 计算滑点（基点）
    if reference_price > 0 and average > 0:
        if side.lower() == "buy":
            slippage = (average - reference_price) / reference_price * 10_000
        else:
            slippage = (reference_price - average) / reference_price * 10_000
    else:
        slippage = 0.0

    return SimulatedFill(
        requested_quantity=requested,
        filled_quantity=filled,
        average_price=average,
        notional=notional,
        worst_price=worst_price,
        slippage_bps=max(slippage, 0.0),
        enough_liquidity=filled + 1e-12 >= requested,
    )


# ---------------------------------------------------------------------------
# L2 深度解析
# ---------------------------------------------------------------------------

def parse_l2_levels(levels: Any) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """解析 L2 深度数据为买卖两侧档位列表。

    支持两种格式：
    - ``[bids, asks]`` 其中每侧为 ``[{px, sz}, ...]`` 或 ``[[price, size], ...]``

    参数:
        levels: L2 深度原始数据。

    返回:
        ``(bids, asks)`` 元组，每侧为 ``[(price, size), ...]`` 列表。
    """
    if not levels or len(levels) < 2:
        return [], []
    return _parse_side(levels[0]), _parse_side(levels[1])


# 向后兼容别名
parse_hyperliquid_levels = parse_l2_levels


def _parse_side(rows: Any) -> list[tuple[float, float]]:
    """解析单侧档位数据。

    支持 dict 格式（``{px, sz}``）和 tuple/list 格式（``[price, size]``）。
    """
    parsed = []
    for row in rows or []:
        if isinstance(row, dict):
            price = row.get("px")
            size = row.get("sz")
        else:
            try:
                price, size = row[0], row[1]
            except Exception:
                continue
        try:
            parsed.append((float(price), float(size)))
        except (TypeError, ValueError):
            continue
    return parsed


# 全局订单簿缓存单例
order_book_cache = OrderBookCache(namespace="shared")


def _book_json(book: OrderBook) -> str:
    time_value = lambda value: value.isoformat() if isinstance(value, datetime) else value
    return json.dumps({
        "platform": book.platform, "symbol": book.symbol,
        "bids": [[item.price, item.size] for item in book.bids],
        "asks": [[item.price, item.size] for item in book.asks],
        "source": book.source, "local_recv_ts": time_value(book.local_recv_ts),
        "exchange_ts": time_value(book.exchange_ts),
    }, ensure_ascii=False, separators=(",", ":"))


def _book_from_json(raw: str) -> OrderBook:
    data = json.loads(raw)
    parse_time = lambda value: datetime.fromisoformat(value) if isinstance(value, str) else value
    return OrderBook(
        platform=data["platform"], symbol=data["symbol"],
        bids=tuple(BookLevel(float(row[0]), float(row[1])) for row in data.get("bids", [])),
        asks=tuple(BookLevel(float(row[0]), float(row[1])) for row in data.get("asks", [])),
        source=data["source"], local_recv_ts=parse_time(data["local_recv_ts"]),
        exchange_ts=parse_time(data.get("exchange_ts")),
    )
