"""
行情数据管理模块
==================

管理多源行情数据的采集、缓存和分发：

- **Paper 模式** — 通过 Hyperliquid / MT5 适配器轮询获取模拟行情
- **Live 模式** — Hyperliquid WebSocket l2Book 实时推送 + MT5 symbol_info_tick 轮询
- **Nautilus 模式** — 通过 NautilusTrader 适配器只读同步第三方 venue 行情

核心类 ``MarketDataManager`` 负责启动/停止各行情线程，
并通过 ``quote_cache`` / ``order_book_cache`` 共享行情数据。

使用 ``ensure_mt5_connected`` 统一 MT5 连接初始化，
使用 ``db_session`` 替代 ``SessionLocal + try/except``，
使用 ``get_logger`` 替代 ``from loguru import logger``。

使用方式::

    from app.workers.market_data import market_data_manager

    market_data_manager.start()   # 启动行情线程
    market_data_manager.stop()    # 停止行情线程
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import OperationalError

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.adapters.venue import build_market_adapter, mapping_leg
from app.config.settings import get_settings
from app.core.db_session import db_session
from app.core.logging import get_logger
from app.core.mt5_bootstrap import ensure_mt5_connected
from app.market.orderbook import order_book_cache, parse_hyperliquid_levels
from app.market.quotes import quote_cache
from app.market.symbols import enabled_mappings

logger = get_logger(__name__)


class MarketDataManager:
    """行情数据管理器 —— 管理多源行情采集线程的生命周期。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._running = False

    def start(self) -> None:
        """根据配置启动对应的行情采集线程。"""
        if self._running:
            return
        self._running = True
        settings = get_settings()
        if settings.quote.source_mode == "live":
            self._start_thread("hyperliquid-ws", self._hyperliquid_ws_loop)
            self._start_thread("mt5-polling", self._mt5_polling_loop)
            if settings.nautilus.read_only_sync_enabled:
                self._start_thread("nautilus-polling", self._nautilus_polling_loop)
        else:
            self._start_thread("paper-quotes", self._paper_loop)

    def stop(self) -> None:
        """停止所有行情采集线程。"""
        self._stop.set()
        self._running = False

    def wait_until_seeded(self, timeout_seconds: float = 3.0) -> None:
        """等待行情数据就绪（每个品种至少有一条报价）。"""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                with db_session() as db:
                    mappings = enabled_mappings(db)
                    symbols = [item.symbol for item in mappings]
            except OperationalError as exc:
                logger.warning("启动等待行情时读取品种映射失败，继续等待: {}", exc)
                mappings = []
                symbols = []
            if symbols and all(_mapping_quotes_seeded(item) for item in mappings):
                return
            time.sleep(0.05)

    def _start_thread(self, name: str, target) -> None:
        """启动一个守护线程。"""
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    # ------------------------------------------------------------------
    # Paper 模式行情循环
    # ------------------------------------------------------------------

    def _paper_loop(self) -> None:
        """Paper 模式行情轮询 —— 通过适配器获取模拟报价。"""
        settings = get_settings()
        interval = max(settings.quote.paper_quote_interval_ms, 50) / 1000
        hyperliquid = HyperliquidAdapter(live=False)
        mt5 = MT5Adapter(live=False)
        while not self._stop.is_set():
            try:
                with db_session() as db:
                    mappings = enabled_mappings(db)
            except OperationalError as exc:
                logger.warning("读取品种映射失败，下一轮重试: {}", exc)
                time.sleep(interval)
                continue
            for mapping in mappings:
                try:
                    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
                    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
                    leg_a = hyperliquid.get_ticker(leg_a_symbol) if leg_a_venue == "hyperliquid" else mt5.get_ticker(leg_a_symbol) if leg_a_venue == "mt5" else build_market_adapter(leg_a_venue).get_ticker(leg_a_symbol)
                    leg_b = hyperliquid.get_ticker(leg_b_symbol) if leg_b_venue == "hyperliquid" else mt5.get_ticker(leg_b_symbol) if leg_b_venue == "mt5" else build_market_adapter(leg_b_venue).get_ticker(leg_b_symbol)
                    if leg_a_venue == "hyperliquid":
                        _put_synthetic_l2(mapping.symbol, leg_a.bid, leg_a.ask, leg_a.depth_notional, "paper")
                    quote_cache.put(leg_a_venue, mapping.symbol, leg_a.bid, leg_a.ask, leg_a.depth_notional, "paper", leg_a.timestamp)
                    quote_cache.put(leg_b_venue, mapping.symbol, leg_b.bid, leg_b.ask, leg_b.depth_notional, "paper", leg_b.timestamp)
                except Exception as exc:
                    logger.warning("Paper 行情更新失败: {}; {}", mapping.symbol, exc)
            time.sleep(interval)

    # ------------------------------------------------------------------
    # MT5 行情轮询
    # ------------------------------------------------------------------

    def _mt5_polling_loop(self) -> None:
        """MT5 行情轮询 —— 使用 ``ensure_mt5_connected`` 初始化连接。"""
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            logger.error("MetaTrader5 包不可用: {}", exc)
            return
        settings = get_settings()
        interval = max(settings.quote.mt5_quote_poll_interval_ms, 50) / 1000
        if not ensure_mt5_connected(
            login=settings.mt5.login or None,
            password=settings.mt5.password or None,
            server=settings.mt5.server or None,
        ):
            logger.error("MT5 initialize 失败: {}", mt5.last_error())
            return
        try:
            while not self._stop.is_set():
                try:
                    with db_session() as db:
                        mappings = enabled_mappings(db)
                except OperationalError as exc:
                    logger.warning("MT5 行情线程读取品种映射失败，下一轮重试: {}", exc)
                    time.sleep(interval)
                    continue
                for mapping in mappings:
                    if "mt5" not in {mapping.leg_a_venue, mapping.leg_b_venue}:
                        continue
                    mt5_symbol = mapping.leg_a_symbol if mapping.leg_a_venue == "mt5" else mapping.leg_b_symbol
                    try:
                        mt5.symbol_select(mt5_symbol, True)
                        tick = mt5.symbol_info_tick(mt5_symbol)
                        if not tick:
                            continue
                        exchange_ts = datetime.utcfromtimestamp(getattr(tick, "time_msc", 0) / 1000) if getattr(tick, "time_msc", 0) else None
                        quote_cache.put("mt5", mapping.symbol, tick.bid, tick.ask, 0.0, "mt5_symbol_info_tick", exchange_ts)
                    except Exception as exc:
                        logger.warning("MT5 行情更新失败: {} {}; {}", mapping.symbol, mt5_symbol, exc)
                time.sleep(interval)
        finally:
            mt5.shutdown()

    # ------------------------------------------------------------------
    # Hyperliquid WebSocket 行情
    # ------------------------------------------------------------------

    def _hyperliquid_ws_loop(self) -> None:
        """Hyperliquid l2Book WebSocket 行情主循环。"""
        asyncio.run(
            self._hyperliquid_l2book_main(
                fast=get_settings().hyperliquid.l2book_fast_enabled,
                hip3_only=False,
                source="hyperliquid_l2Book_fast" if get_settings().hyperliquid.l2book_fast_enabled else "hyperliquid_l2Book",
            )
        )

    def _hyperliquid_fast_l2book_loop(self) -> None:
        """Hyperliquid l2Book 快速通道 WebSocket 行情循环。"""
        asyncio.run(
            self._hyperliquid_l2book_main(
                fast=True,
                hip3_only=False,
                source="hyperliquid_l2Book_fast",
            )
        )

    async def _hyperliquid_l2book_main(self, *, fast: bool, hip3_only: bool, source: str) -> None:
        """Hyperliquid l2Book WebSocket 连接和消息处理主循环。"""
        try:
            import websockets  # type: ignore
        except Exception as exc:
            logger.error("websockets 包不可用: {}", exc)
            return
        settings = get_settings()
        while not self._stop.is_set():
            by_hl_symbol = self._load_hyperliquid_symbol_map(hip3_only=hip3_only)
            if not by_hl_symbol:
                await asyncio.sleep(2)
                continue
            try:
                async with websockets.connect(settings.hyperliquid.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    subscribed: set[str] = set()
                    for coin in by_hl_symbol:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                        subscribed.add(coin)
                    mode = "fast" if fast else "default"
                    scope = "HIP-3" if hip3_only else "all"
                    logger.info("Hyperliquid l2Book WS 已订阅: {} 个品种, mode={}, scope={}", len(by_hl_symbol), mode, scope)
                    last_refresh = time.monotonic()
                    while not self._stop.is_set():
                        # 定期刷新品种映射
                        if time.monotonic() - last_refresh >= 2:
                            next_by_hl_symbol = self._load_hyperliquid_symbol_map(hip3_only=hip3_only)
                            for coin in set(next_by_hl_symbol) - subscribed:
                                await ws.send(json.dumps({"method": "subscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                                subscribed.add(coin)
                                logger.info("Hyperliquid l2Book WS 动态订阅: {}", coin)
                            removed = subscribed - set(next_by_hl_symbol)
                            for coin in removed:
                                await ws.send(json.dumps({"method": "unsubscribe", "subscription": l2book_subscription(coin, fast=fast)}))
                                subscribed.remove(coin)
                                logger.info("Hyperliquid l2Book WS 取消订阅: {}", coin)
                            by_hl_symbol = next_by_hl_symbol
                            last_refresh = time.monotonic()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if self._stop.is_set():
                            break
                        try:
                            self._handle_hyperliquid_message(json.loads(raw), by_hl_symbol, source)
                        except Exception as exc:
                            logger.warning("Hyperliquid WS 消息处理失败: {}", exc)
            except Exception as exc:
                logger.error("Hyperliquid WS 断开，准备重连: {}", exc)
                await asyncio.sleep(2)

    def _load_hyperliquid_symbol_map(self, *, hip3_only: bool) -> dict[str, str]:
        """从数据库加载 Hyperliquid 品种映射。"""
        try:
            with db_session() as db:
                mappings = enabled_mappings(db)
                return hyperliquid_symbol_map(mappings, hip3_only=hip3_only)
        except OperationalError as exc:
            logger.warning("Hyperliquid WS 读取品种映射失败，保持现有订阅: {}", exc)
            return {}
        except Exception as exc:
            logger.warning("Hyperliquid WS 构建品种映射失败: {}", exc)
            return {}

    # ------------------------------------------------------------------
    # Nautilus 只读行情轮询
    # ------------------------------------------------------------------

    def _nautilus_polling_loop(self) -> None:
        """Nautilus 只读行情轮询 —— 从第三方 venue 获取行情。"""
        settings = get_settings()
        interval = max(settings.nautilus.quote_poll_interval_ms, 250) / 1000
        while not self._stop.is_set():
            try:
                with db_session() as db:
                    mappings = enabled_mappings(db)
            except OperationalError as exc:
                logger.warning("Nautilus 行情线程读取品种映射失败，下一轮重试: {}", exc)
                time.sleep(interval)
                continue
            for mapping in mappings:
                for index in ("a", "b"):
                    venue, venue_symbol = mapping_leg(mapping, index)
                    if venue in {"hyperliquid", "mt5"}:
                        continue
                    try:
                        ticker = build_market_adapter(venue, live=True).get_ticker(venue_symbol)
                        quote_cache.put(venue, mapping.symbol, ticker.bid, ticker.ask, ticker.depth_notional, "nautilus_read_only", ticker.timestamp)
                    except Exception as exc:
                        logger.warning("Nautilus 行情更新失败: {} {}:{}; {}", mapping.symbol, venue, venue_symbol, exc)
            time.sleep(interval)

    # ------------------------------------------------------------------
    # Hyperliquid 消息处理
    # ------------------------------------------------------------------

    def _handle_hyperliquid_message(self, payload: dict[str, Any], by_hl_symbol: dict[str, str], source: str = "hyperliquid_l2Book") -> None:
        """处理 Hyperliquid WebSocket l2Book 消息。"""
        channel = payload.get("channel")
        data = payload.get("data") or {}
        if channel != "l2Book":
            return
        coin = data.get("coin")
        symbol = by_hl_symbol.get(coin)
        levels = data.get("levels") or []
        if not symbol or len(levels) < 2 or not levels[0] or not levels[1]:
            return
        self._write_hyperliquid_levels(symbol, levels, source, _exchange_time_from_hyperliquid_ms(data.get("time")))

    def _write_hyperliquid_levels(self, symbol: str, levels: Any, source: str, exchange_ts: datetime | None = None) -> None:
        """将 Hyperliquid l2Book 数据写入报价缓存和订单簿缓存。"""
        bids, asks = parse_hyperliquid_levels(levels)
        if not bids or not asks:
            return
        bid, bid_size = bids[0]
        ask, ask_size = asks[0]
        depth_notional = min(bid * bid_size, ask * ask_size)
        order_book_cache.put("hyperliquid", symbol, bids, asks, source, exchange_ts)
        quote_cache.put("hyperliquid", symbol, bid, ask, depth_notional, source, exchange_ts)


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------

def _exchange_time_from_hyperliquid_ms(value: Any) -> datetime | None:
    """将 Hyperliquid 交易所时间戳（毫秒）转换为 naive UTC datetime。"""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)


def l2book_subscription(coin: str, *, fast: bool) -> dict[str, Any]:
    """构建 Hyperliquid l2Book 订阅消息。"""
    subscription: dict[str, Any] = {"type": "l2Book", "coin": coin}
    if fast:
        subscription["fast"] = True
    return subscription


def hyperliquid_symbol_map(mappings, *, hip3_only: bool) -> dict[str, str]:
    """从品种映射中提取 Hyperliquid 品种名 → 通用 symbol 的映射。"""
    rows: dict[str, str] = {}
    for item in mappings:
        for index in ("a", "b"):
            venue, venue_symbol = mapping_leg(item, index)
            if venue != "hyperliquid":
                continue
            if hip3_only and ":" not in venue_symbol:
                continue
            rows[venue_symbol] = item.symbol
    return rows


# 全局行情数据管理器单例
market_data_manager = MarketDataManager()


def _put_synthetic_l2(symbol: str, bid: float, ask: float, depth_notional: float, source: str) -> None:
    """生成合成 L2 订单簿（Paper 模式下使用）。"""
    levels = 10
    mid = max((bid + ask) / 2, 1e-12)
    step = max((ask - bid) / max(levels, 1), mid * 0.00002)
    level_notional = max(depth_notional, mid * 1000) / levels
    bids = [(bid - step * index, level_notional / max(bid - step * index, 1e-12)) for index in range(levels)]
    asks = [(ask + step * index, level_notional / max(ask + step * index, 1e-12)) for index in range(levels)]
    order_book_cache.put("hyperliquid", symbol, bids, asks, source)


def _mapping_quotes_seeded(mapping) -> bool:
    """检查品种的两个 venue 是否都已有报价数据。"""
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_b_venue, _ = mapping_leg(mapping, "b")
    return bool(quote_cache.latest(leg_a_venue, mapping.symbol) and quote_cache.latest(leg_b_venue, mapping.symbol))
