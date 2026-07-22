"""
报价缓存与同步模块
====================

提供跨交易所报价的存储、查询和同步对齐功能：

- :class:`Quote` —— 单个交易所的单次报价快照
- :class:`SynchronizedQuote` —— 两腿对齐后的同步报价
- :class:`QuoteCache` —— 线程安全的报价缓存（支持历史查询）
- :class:`QuoteSynchronizer` —— 跨交易所报价时间对齐器

报价缓存使用 Redis 保存最新值与 Stream 历史，支持 API 与 Worker 按
``(platform, symbol)`` 键共享报价。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Iterable, Literal, Mapping

from app.core.logging import get_logger
from app.core.performance import elapsed_ms, log_slow_operation
from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now

# 同步模式类型：loose（宽松）或 strict（严格）
SyncMode = Literal["loose", "strict"]
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    """单个交易所的单次报价快照。

    属性:
        platform: 平台标识（如 ``"hyperliquid"`` / ``"mt5"``）。
        symbol: 品种名称。
        bid: 买一价。
        ask: 卖一价。
        depth_notional: 可用深度名义值（USD）。
        exchange_ts: 交易所时间戳（可选，部分交易所不提供）。
        local_recv_ts: 本地接收时间（naive UTC）。
        source: 数据来源标识。
        sequence: 全局递增序列号。
    """

    platform: str
    symbol: str
    bid: float
    ask: float
    depth_notional: float
    exchange_ts: object | None
    local_recv_ts: object
    source: str
    sequence: int
    bid_depth_notional: float = 0.0
    ask_depth_notional: float = 0.0

    @property
    def mid(self) -> float:
        """中间价 = (bid + ask) / 2。"""
        return (self.bid + self.ask) / 2


@dataclass(frozen=True)
class SynchronizedQuote:
    """两腿对齐后的同步报价。

    属性:
        symbol: 品种名称。
        leg_a: A 腿报价（通常为 Hyperliquid）。
        leg_b: B 腿报价（通常为 MT5）。
        time_diff_ms: 两腿报价时间差（毫秒）。
        max_age_ms: 两腿报价中较老的年龄（毫秒）。
        mode: 同步模式（``"loose"`` 或 ``"strict"``）。
    """

    symbol: str
    leg_a: Quote
    leg_b: Quote
    time_diff_ms: float
    max_age_ms: float
    mode: SyncMode

    # 向后兼容别名
    @property
    def hyperliquid(self) -> Quote:
        """向后兼容：返回 A 腿报价。"""
        return self.leg_a

    @property
    def mt5(self) -> Quote:
        """向后兼容：返回 B 腿报价。"""
        return self.leg_b


# ---------------------------------------------------------------------------
# 报价缓存
# ---------------------------------------------------------------------------

class QuoteCache:
    """基于 Redis 的共享报价缓存。

    按 ``(platform, symbol)`` 键存储报价历史，支持：
    - 写入新报价（自动维护历史长度上限）
    - 查询最新报价
    - 查询历史报价序列
    - 查询已缓存的品种列表
    """

    def __init__(self, max_history: int = 5000, *, namespace: str | None = None) -> None:
        self.max_history = max_history
        # 运行时全局实例使用固定命名空间，以便 API 与 Worker 共享；测试可保持隔离。
        self._namespace = namespace or uuid.uuid4().hex

    def _latest_key(self, platform: str, symbol: str) -> str:
        return redis_key("quotes", self._namespace, "latest", platform, symbol)

    def _history_key(self, platform: str, symbol: str) -> str:
        return redis_key("quotes", self._namespace, "history", platform, symbol)

    def _symbols_key(self) -> str:
        return redis_key("quotes", self._namespace, "symbols")

    def put(
        self,
        platform: str,
        symbol: str,
        bid: float,
        ask: float,
        depth_notional: float,
        source: str,
        exchange_ts=None,
        local_recv_ts=None,
        bid_depth_notional: float | None = None,
        ask_depth_notional: float | None = None,
    ) -> Quote:
        """写入一条新报价。

        参数:
            platform: 平台标识。
            symbol: 品种名称。
            bid: 买一价。
            ask: 卖一价。
            depth_notional: 深度名义值。
            source: 数据来源。
            exchange_ts: 交易所时间戳（可选）。

        返回:
            创建的 Quote 实例（含全局序列号和本地接收时间）。
        """
        client = redis_client()
        sequence = int(client.incr(redis_key("quotes", "sequence")))
        quote = Quote(
            platform=platform, symbol=symbol, bid=float(bid), ask=float(ask),
            depth_notional=float(depth_notional), exchange_ts=exchange_ts,
            local_recv_ts=local_recv_ts or utc_now(), source=source, sequence=sequence,
            bid_depth_notional=float(depth_notional if bid_depth_notional is None else bid_depth_notional),
            ask_depth_notional=float(depth_notional if ask_depth_notional is None else ask_depth_notional),
        )
        serialized = _quote_json(quote)
        pipe = client.pipeline(transaction=False)
        pipe.set(self._latest_key(platform, symbol), serialized)
        pipe.xadd(self._history_key(platform, symbol), {"data": serialized}, maxlen=self.max_history, approximate=True)
        pipe.sadd(self._symbols_key(), symbol)
        pipe.execute()
        return quote

    def latest(self, platform: str, symbol: str) -> Quote | None:
        """获取指定平台和品种的最新报价。"""
        started = perf_counter()
        raw = redis_client().get(self._latest_key(platform, symbol))
        duration_ms = elapsed_ms(started)
        log_slow_operation(
            logger, "redis", "quote_latest", duration_ms,
            platform=platform, symbol=symbol,
        )
        return _quote_from_json(raw) if raw else None

    def latest_many(self, requests: Iterable[tuple[str, str]]) -> dict[tuple[str, str], Quote]:
        """一次 Redis MGET 读取一轮扫描需要的全部最新报价。"""
        pairs = list(dict.fromkeys((str(platform), str(symbol)) for platform, symbol in requests))
        if not pairs:
            return {}
        started = perf_counter()
        raws = redis_client().mget([self._latest_key(platform, symbol) for platform, symbol in pairs])
        result = {
            pair: _quote_from_json(raw)
            for pair, raw in zip(pairs, raws)
            if raw is not None
        }
        log_slow_operation(
            logger, "redis", "quote_latest_many", elapsed_ms(started),
            request_count=len(pairs), cache_hits=len(result),
        )
        return result

    def history(self, platform: str, symbol: str) -> list[Quote]:
        """获取指定平台和品种的历史报价列表。"""
        rows = redis_client().xrange(self._history_key(platform, symbol))
        return [_quote_from_json(fields["data"]) for _, fields in rows]

    def symbols(self) -> list[str]:
        """获取所有已缓存的品种名称（去重、排序）。"""
        return sorted(redis_client().smembers(self._symbols_key()))


# 全局报价缓存单例
quote_cache = QuoteCache(namespace="shared")


def _json_time(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _quote_json(quote: Quote) -> str:
    return json.dumps({
        "platform": quote.platform, "symbol": quote.symbol, "bid": quote.bid, "ask": quote.ask,
        "depth_notional": quote.depth_notional, "exchange_ts": _json_time(quote.exchange_ts),
        "local_recv_ts": _json_time(quote.local_recv_ts), "source": quote.source, "sequence": quote.sequence,
        "bid_depth_notional": quote.bid_depth_notional,
        "ask_depth_notional": quote.ask_depth_notional,
    }, ensure_ascii=False, separators=(",", ":"))


def _parse_time(value):
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _quote_from_json(raw: str) -> Quote:
    data = json.loads(raw)
    data["exchange_ts"] = _parse_time(data.get("exchange_ts"))
    data["local_recv_ts"] = _parse_time(data["local_recv_ts"])
    data.setdefault("bid_depth_notional", float(data.get("depth_notional") or 0.0))
    data.setdefault("ask_depth_notional", float(data.get("depth_notional") or 0.0))
    return Quote(**data)


# ---------------------------------------------------------------------------
# 报价同步器
# ---------------------------------------------------------------------------

class QuoteSynchronizer:
    """跨交易所报价时间对齐器。

    检查两腿报价的时间差和年龄是否在允许范围内，
    满足条件时返回同步报价对象，否则返回 None 和原因描述。
    """

    def __init__(self, cache: QuoteCache) -> None:
        self.cache = cache

    def synchronized(
        self,
        symbol: str,
        mode: SyncMode,
        max_time_diff_ms: int,
        max_age_ms: int,
        *,
        leg_a_venue: str = "hyperliquid",
        leg_b_venue: str = "mt5",
        snapshot: Mapping[tuple[str, str], Quote] | None = None,
    ) -> tuple[SynchronizedQuote | None, str]:
        """获取同步对齐的两腿报价。

        检查条件：
        1. 两腿均有报价
        2. 报价年龄不超过 max_age_ms
        3. 时间差不超过 max_time_diff_ms
        4. 报价值合法（bid > 0, ask > 0, bid <= ask）

        参数:
            symbol: 品种名称。
            mode: 同步模式（``"loose"`` 或 ``"strict"``）。
            max_time_diff_ms: 最大允许时间差（毫秒）。
            max_age_ms: 最大允许报价年龄（毫秒）。
            leg_a_venue: A 腿 venue，默认 ``"hyperliquid"``。
            leg_b_venue: B 腿 venue，默认 ``"mt5"``。

        返回:
            ``(SynchronizedQuote, "")`` 成功时；
            ``(None, 原因描述)`` 失败时。
        """
        if snapshot is None:
            hl = self.cache.latest(leg_a_venue, symbol)
            mt5 = self.cache.latest(leg_b_venue, symbol)
        else:
            hl = snapshot.get((leg_a_venue, symbol))
            mt5 = snapshot.get((leg_b_venue, symbol))
        if not hl or not mt5:
            return None, "缺少实时行情"

        now = utc_now()
        hl_age = (now - hl.local_recv_ts).total_seconds() * 1000
        mt5_age = (now - mt5.local_recv_ts).total_seconds() * 1000
        max_age = max(hl_age, mt5_age)
        if max_age > max_age_ms:
            return None, f"行情过期，最大延迟 {max_age:.0f}ms"

        time_diff = abs((hl.local_recv_ts - mt5.local_recv_ts).total_seconds() * 1000)
        if time_diff > max_time_diff_ms:
            return None, f"行情未对齐，时间差 {time_diff:.0f}ms"

        # 报价合法性检查
        if hl.bid <= 0 or hl.ask <= 0 or mt5.bid <= 0 or mt5.ask <= 0:
            return None, "报价异常"
        if hl.bid > hl.ask or mt5.bid > mt5.ask:
            return None, "bid/ask 反转"

        return SynchronizedQuote(
            symbol=symbol,
            leg_a=hl,
            leg_b=mt5,
            time_diff_ms=time_diff,
            max_age_ms=max_age,
            mode=mode,
        ), ""


# 全局报价同步器单例
quote_synchronizer = QuoteSynchronizer(quote_cache)
