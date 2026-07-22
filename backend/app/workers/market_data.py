"""统一原生交易所行情采集、订阅维护和共享缓存投影。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy.exc import OperationalError

from app.config.settings import get_settings
from app.core.db_session import db_session
from app.core.logging import get_logger
from app.market.orderbook import parse_hyperliquid_levels
from app.market.quotes import quote_cache
from app.market.symbols import enabled_mappings
from app.venues.manager import native_venue_manager
from app.venues.paper import PaperConnector

logger = get_logger(__name__)
SUPPORTED_VENUES = {"hyperliquid", "mt5", "binance"}


class MarketDataManager:
    """维护公共行情连接，并把统一模型投影到现有行情缓存。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paper_connectors: dict[str, PaperConnector] = {}

    def start(self) -> None:
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="native-market-data", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def wait_until_seeded(self, timeout_seconds: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout_seconds
        mappings = []
        while time.monotonic() < deadline:
            try:
                with db_session() as db:
                    mappings = enabled_mappings(db)
            except OperationalError as exc:
                logger.warning("启动等待行情时读取品种映射失败，继续等待: {}", exc)
                mappings = []
            if mappings and all(_mapping_quotes_seeded(item) for item in mappings):
                logger.info("启动行情已就绪: mappings={}", len(mappings))
                return True
            self._stop.wait(0.05)
        pending = [item.symbol for item in mappings if not _mapping_quotes_seeded(item)]
        logger.warning("启动行情等待超时，后台继续重试: pending={}", ",".join(pending) or "none")
        return False

    def _run(self) -> None:
        settings = get_settings()
        paper = settings.quote.source_mode != "live"
        interval_ms = settings.quote.paper_quote_interval_ms if paper else min(
            settings.quote.mt5_quote_poll_interval_ms,
            250,
        )
        interval = max(interval_ms, 50) / 1000
        while not self._stop.is_set():
            try:
                with db_session() as db:
                    mappings = enabled_mappings(db)
                self._refresh(mappings, paper=paper)
            except OperationalError as exc:
                logger.warning("行情线程读取品种映射失败，下一轮重试: {}", exc)
            except Exception as exc:
                logger.exception("行情采集循环失败，下一轮重试: {}", exc)
            self._stop.wait(interval)

    def _refresh(self, mappings, *, paper: bool) -> None:
        symbols_by_venue: dict[str, set[str]] = defaultdict(set)
        for mapping in mappings:
            for index in ("a", "b"):
                venue, venue_symbol = mapping_leg(mapping, index)
                if venue in SUPPORTED_VENUES and venue_symbol:
                    symbols_by_venue[venue].add(venue_symbol)

        connectors = {}
        for venue, symbols in symbols_by_venue.items():
            try:
                connector = self._paper_connector(venue) if paper else native_venue_manager.connector_for(venue, "live")
                connector.start()
                if not paper:
                    connector.subscribe_market_data(sorted(symbols))
                connectors[venue] = connector
            except Exception as exc:
                # 单一交易所故障必须隔离，避免阻断其他交易所的行情投影。
                logger.warning("交易所行情连接失败，本轮跳过: venue={}, error={}", venue, exc)

        for mapping in mappings:
            for index in ("a", "b"):
                venue, venue_symbol = mapping_leg(mapping, index)
                connector = connectors.get(venue)
                if connector is None:
                    continue
                try:
                    ticker = connector.get_ticker(venue_symbol)
                    bid_depth_notional = ticker.bid * ticker.bid_quantity
                    ask_depth_notional = ticker.ask * ticker.ask_quantity
                    depth_notional = min(bid_depth_notional, ask_depth_notional)
                    quote_cache.put(
                        venue,
                        mapping.symbol,
                        float(ticker.bid),
                        float(ticker.ask),
                        float(max(depth_notional, Decimal("0"))),
                        "paper" if paper else f"native_{venue}",
                        ticker.exchange_time,
                        local_recv_ts=ticker.received_at,
                        bid_depth_notional=float(max(bid_depth_notional, Decimal("0"))),
                        ask_depth_notional=float(max(ask_depth_notional, Decimal("0"))),
                    )
                except Exception as exc:
                    logger.warning("行情更新失败: mapping={}, venue={}, symbol={}, error={}", mapping.symbol, venue, venue_symbol, exc)

    def _paper_connector(self, venue: str) -> PaperConnector:
        connector = self._paper_connectors.get(venue)
        if connector is None:
            connector = PaperConnector(venue=venue)
            self._paper_connectors[venue] = connector
        return connector

    def _handle_hyperliquid_message(
        self,
        payload: dict[str, Any],
        by_hl_symbol: dict[str, str],
        source: str = "native_hyperliquid",
    ) -> None:
        """兼容测试和诊断入口；正式行情由原生 WS runtime 处理。"""
        data = payload.get("data") or {}
        if payload.get("channel") != "l2Book":
            return
        symbol = by_hl_symbol.get(str(data.get("coin") or ""))
        levels = data.get("levels") or []
        if not symbol or len(levels) < 2:
            return
        bids, asks = parse_hyperliquid_levels(levels)
        if not bids or not asks:
            return
        exchange_time = _exchange_time_from_hyperliquid_ms(data.get("time"))
        depth = min(bids[0][0] * bids[0][1], asks[0][0] * asks[0][1])
        quote_cache.put("hyperliquid", symbol, bids[0][0], asks[0][0], depth, source, exchange_time)


def mapping_leg(mapping, index: str) -> tuple[str, str]:
    """从映射对象提取指定腿的交易所和交易所品种。"""
    if index == "a":
        venue = str(getattr(mapping, "leg_a_venue", "") or "hyperliquid").strip().lower()
        symbol = str(
            getattr(mapping, "leg_a_symbol", "")
            or getattr(mapping, "leg_a_venue_symbol", "")
            or getattr(mapping, "symbol", "")
        )
        return venue, symbol
    venue = str(getattr(mapping, "leg_b_venue", "") or "mt5").strip().lower()
    symbol = str(
        getattr(mapping, "leg_b_symbol", "")
        or getattr(mapping, "mt5_symbol", "")
        or getattr(mapping, "symbol", "")
    )
    return venue, symbol


def _exchange_time_from_hyperliquid_ms(value: Any) -> datetime | None:
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).replace(tzinfo=None)


def hyperliquid_symbol_map(mappings, *, hip3_only: bool) -> dict[str, str]:
    rows: dict[str, str] = {}
    for item in mappings:
        for index in ("a", "b"):
            venue, venue_symbol = mapping_leg(item, index)
            if venue != "hyperliquid" or (hip3_only and ":" not in venue_symbol):
                continue
            rows[venue_symbol] = item.symbol
    return rows


def _mapping_quotes_seeded(mapping) -> bool:
    leg_a_venue, _ = mapping_leg(mapping, "a")
    leg_b_venue, _ = mapping_leg(mapping, "b")
    return bool(quote_cache.latest(leg_a_venue, mapping.symbol) and quote_cache.latest(leg_b_venue, mapping.symbol))


market_data_manager = MarketDataManager()
