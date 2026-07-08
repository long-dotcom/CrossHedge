"""
NautilusTrader 长期运行时管理
=============================

除 MT5 / Hyperliquid 以外的交易所必须通过这里接入 NautilusTrader live runtime。
本模块不再调用交易所专属 HTTP helper；行情、账户、持仓、订单和成交均从
TradingNode 的 cache/report/exec engine 状态读写。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, InvalidOperation
from enum import Enum
from threading import RLock, Thread
import time
from typing import Any, Callable

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.core.logging import get_logger
from app.db.models import ExchangeCredential
from app.exchanges.credentials import decrypt_credentials

logger = get_logger(__name__)


class NautilusTradeMode(str, Enum):
    """Nautilus 非原生交易所的交易模式。"""

    PAPER_PROBE = "paper_probe"
    LIVE = "live"


class NautilusRuntimeStatus(str, Enum):
    """单个 venue runtime 生命周期状态。"""

    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass(frozen=True)
class NautilusRuntimeKey:
    """Nautilus runtime 缓存键。"""

    venue: str
    environment: str
    credential_id: int | None


@dataclass(frozen=True)
class NautilusVenueSpec:
    """项目内已接入的 Nautilus live venue 配置。"""

    venue: str
    client_name: str
    builder: Callable[[ExchangeCredential, dict[str, Any]], "NautilusNodeBuild"]


@dataclass(frozen=True)
class NautilusNodeBuild:
    """创建 TradingNode 所需的最小配置。"""

    config: Any
    data_factory: Any
    exec_factory: Any


@dataclass(frozen=True)
class NautilusInstrumentSpec:
    """从 Nautilus instrument 提取出的下单规格。"""

    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    price_increment: Decimal


class NautilusVenueRuntime:
    """单个 venue 的 Nautilus TradingNode runtime。"""

    def __init__(self, credential: ExchangeCredential) -> None:
        self.credential = credential
        self.venue = _venue(credential.venue)
        self.environment = str(credential.environment or "sandbox").strip().lower()
        self.status = NautilusRuntimeStatus.NOT_LOADED
        self.last_error = ""
        self.node: Any = None
        self.thread: Thread | None = None
        self._quote_subscriptions: set[str] = set()
        self._mark_price_subscriptions: set[str] = set()
        self._lock = RLock()
        self._import_error = _nautilus_import_error()

    def ensure_loaded(self) -> None:
        """构建并启动 Nautilus TradingNode。"""
        with self._lock:
            if self.status == NautilusRuntimeStatus.RUNNING:
                return
            if self.status == NautilusRuntimeStatus.FAILED:
                raise RuntimeError(self.last_error)
            if self._import_error:
                self._fail(f"NautilusTrader 可选依赖不可用: {self._import_error}")
            spec = SUPPORTED_NAUTILUS_VENUES.get(self.venue)
            if spec is None:
                self._fail(f"Nautilus venue {self.venue} 尚未接入 Nautilus live runtime")
            self.status = NautilusRuntimeStatus.LOADING
            try:
                credentials = decrypt_credentials(self.credential)
                build = spec.builder(self.credential, credentials)
                node = _create_trading_node(build.config)
                node.add_data_client_factory(spec.client_name, build.data_factory)
                node.add_exec_client_factory(spec.client_name, build.exec_factory)
                node.build()
                self.node = node
                self.thread = Thread(
                    target=self._run_node,
                    name=f"nautilus-{self.venue}-{self.environment}",
                    daemon=True,
                )
                self.thread.start()
                self.status = NautilusRuntimeStatus.RUNNING
                self.last_error = ""
                logger.info("Nautilus TradingNode 已启动: venue={}, environment={}", self.venue, self.environment)
            except Exception as exc:
                self._dispose_node()
                self._fail(f"Nautilus venue {self.venue} runtime 加载失败: {exc}")

    def stop(self) -> None:
        """停止当前 TradingNode。"""
        with self._lock:
            if not self.node:
                self.status = NautilusRuntimeStatus.NOT_LOADED
                return
            self.status = NautilusRuntimeStatus.STOPPING
            try:
                if hasattr(self.node, "stop"):
                    self.node.stop()
            finally:
                self._dispose_node()
                self.status = NautilusRuntimeStatus.NOT_LOADED

    def get_account(self) -> dict[str, Any]:
        """从 Nautilus cache/report 读取账户信息。"""
        self.ensure_loaded()
        deadline = time.monotonic() + 25.0
        while True:
            account = _cache_account(self.node, self.venue)
            if account is not None:
                return _account_to_dict(account)
            rows = _safe_report(self.node.trader, "generate_account_report")
            if rows:
                return _row_to_dict(rows[-1])
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Nautilus venue {self.venue} 账户状态尚未加载，请等待执行客户端完成账户同步")
            time.sleep(0.25)

    def get_positions(self) -> list[dict[str, Any]]:
        """从 Nautilus cache/report 读取持仓。"""
        self.ensure_loaded()
        deadline = time.monotonic() + 45.0
        while True:
            rows = _cache_positions(self.node, self.venue)
            if not rows:
                rows = [_row_to_dict(row) for row in _safe_report(self.node.trader, "generate_positions_report")]
            positions = [_position_to_dict(self.venue, item) for item in rows if _position_quantity(item) != Decimal("0")]
            if positions or time.monotonic() >= deadline:
                return _dedupe_positions(positions)
            time.sleep(0.25)

    def get_ticker(self, symbol: str) -> dict[str, float]:
        """从 Nautilus cache 读取最新买一/卖一。"""
        self.ensure_loaded()
        instrument_id = self.instrument_id(symbol)
        quote = _cache_quote(self.node, instrument_id)
        if quote is None:
            self._subscribe_quote_ticks(instrument_id)
            book = _cache_order_book(self.node, instrument_id)
            quote = _quote_from_book(book)
        if quote is None:
            raise RuntimeError(f"Nautilus venue {self.venue} 行情已订阅，等待首条 quote tick: {instrument_id}")
        bid = _float(_field(quote, "bid_price", _field(quote, "bid", 0)))
        ask = _float(_field(quote, "ask_price", _field(quote, "ask", 0)))
        bid_size = _float(_field(quote, "bid_size", 0))
        ask_size = _float(_field(quote, "ask_size", 0))
        return {
            "bid": bid,
            "ask": ask,
            "depth_notional": min(bid * bid_size, ask * ask_size) if bid > 0 and ask > 0 else 0.0,
        }

    def get_mark_price(self, symbol: str) -> float:
        """从 Nautilus cache 读取最新标记价格。"""
        self.ensure_loaded()
        instrument_id = self.instrument_id(symbol)
        deadline = time.monotonic() + 2.0
        while True:
            mark_price = _cache_mark_price(self.node, instrument_id)
            value = _mark_price_value(mark_price)
            if value > 0:
                return value
            self._subscribe_mark_prices(instrument_id)
            if time.monotonic() >= deadline:
                return 0.0
            time.sleep(0.1)

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        """从 Nautilus cache 读取订单簿。"""
        self.ensure_loaded()
        instrument_id = self.instrument_id(symbol)
        book = _cache_order_book(self.node, instrument_id)
        return _order_book_to_dict(symbol, book, depth)

    def place_order(
        self,
        order: AdapterOrder,
        mode: NautilusTradeMode,
        *,
        configured_min_base_size: float = 0.0,
    ) -> AdapterOrderResult:
        """通过 Nautilus exec engine 提交订单。"""
        self.ensure_loaded()
        if mode not in {NautilusTradeMode.PAPER_PROBE, NautilusTradeMode.LIVE}:
            return _reject(f"Nautilus 仅支持 paper_probe / live 模式: {mode}")
        if self.credential.read_only:
            return _reject(f"{self.venue} 交易所配置仍为只读，禁止 {mode.value} 下单")
        if not self.credential.encrypted_credentials:
            return _reject(f"{self.venue} 交易所凭证未配置，禁止 {mode.value} 下单")
        try:
            instrument = self.instrument(order.venue_symbol or order.symbol)
            specs = _instrument_specs(instrument)
            reference_price = _reference_price(self.get_ticker(order.venue_symbol or order.symbol), order.side)
            if mode == NautilusTradeMode.PAPER_PROBE:
                real_quantity = _probe_quantity(specs, reference_price, configured_min_base_size)
                ledger_quantity = Decimal(str(order.quantity))
            else:
                _reject_unsupported_order_flags(order)
                real_quantity = _live_quantity(specs, Decimal(str(order.quantity)), reference_price)
                ledger_quantity = real_quantity
            submitted = self._submit_order(order, instrument, real_quantity)
            result = self._order_result(submitted, real_quantity, ledger_quantity, mode)
            if result.success and mode == NautilusTradeMode.PAPER_PROBE:
                result.filled_quantity = float(ledger_quantity)
                result.error_message = (
                    f"Nautilus paper_probe 真实成交量 {real_quantity}，"
                    f"paper 账本成交量 {ledger_quantity}，真实订单 ID {result.external_order_id}"
                )
            return result
        except Exception as exc:
            return _reject(f"Nautilus {mode.value} 下单失败: {exc}")

    def cancel_order(self, order_id: str) -> bool:
        """通过 Nautilus cache 查找订单并发送撤单命令。"""
        self.ensure_loaded()
        order = _cache_order(self.node, order_id)
        if order is None:
            logger.warning("Nautilus cache 中找不到订单，无法撤单: venue={}, order_id={}", self.venue, order_id)
            return False
        try:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.execution.messages import CancelOrder

            command = CancelOrder(
                trader_id=self.node.trader_id,
                strategy_id=_strategy_id(),
                instrument_id=order.instrument_id,
                client_order_id=order.client_order_id,
                command_id=UUID4(),
                ts_init=_clock_timestamp_ns(self.node),
            )
            self.node.kernel.exec_engine.execute(command)
            return True
        except Exception as exc:
            logger.warning("Nautilus 撤单失败: venue={}, order_id={}, error={}", self.venue, order_id, exc)
            return False

    def get_order(self, order_id: str) -> dict[str, Any]:
        """从 Nautilus cache/report 查询订单。"""
        self.ensure_loaded()
        order = _cache_order(self.node, order_id)
        if order is not None:
            return _object_to_dict(order)
        for row in _safe_report(self.node.trader, "generate_orders_report"):
            data = _row_to_dict(row)
            if order_id in {str(data.get("client_order_id") or ""), str(data.get("venue_order_id") or ""), str(data.get("id") or "")}:
                return data
        return {"status": "unknown", "external_order_id": order_id, "message": "Nautilus cache/report 中未找到订单"}

    def get_trades(self, order_id: str) -> list[dict[str, Any]]:
        """从 Nautilus fills report 查询成交。"""
        self.ensure_loaded()
        rows = []
        for row in _safe_report(self.node.trader, "generate_fills_report"):
            data = _row_to_dict(row)
            if order_id in {str(data.get("client_order_id") or ""), str(data.get("venue_order_id") or ""), str(data.get("order_id") or "")}:
                rows.append(data)
        return rows

    def instrument_id(self, symbol: str) -> Any:
        """解析或推断 Nautilus instrument ID。"""
        from nautilus_trader.model.identifiers import InstrumentId

        raw = str(symbol or "").strip()
        if "." in raw:
            return InstrumentId.from_str(raw)
        instrument = self.instrument(raw)
        return instrument.id

    def instrument(self, symbol: str) -> Any:
        """从 Nautilus cache 读取 instrument。"""
        self.ensure_loaded()
        raw = str(symbol or "").strip()
        cache = self.node.cache
        candidates = [raw]
        if "." not in raw:
            candidates.append(f"{_normalize_symbol(raw)}.{self.venue.upper()}")
            candidates.append(f"{_normalize_symbol(raw)}-PERP.{self.venue.upper()}")
        for candidate in candidates:
            try:
                from nautilus_trader.model.identifiers import InstrumentId

                instrument = cache.instrument(InstrumentId.from_str(candidate))
                if instrument:
                    return instrument
            except Exception:
                continue
        for instrument in _cache_instruments(self.node, self.venue):
            if _normalize_symbol(raw) in _normalize_symbol(str(getattr(instrument, "id", ""))):
                return instrument
        raise RuntimeError(f"Nautilus instrument 尚未加载: venue={self.venue}, symbol={symbol}")

    def _submit_order(self, order: AdapterOrder, instrument: Any, quantity: Decimal) -> Any:
        """构造 Nautilus order 并交给 ExecEngine。"""
        from nautilus_trader.common.factories import OrderFactory
        from nautilus_trader.core.uuid import UUID4
        from nautilus_trader.execution.messages import SubmitOrder
        from nautilus_trader.model.enums import OrderSide, TimeInForce
        from nautilus_trader.model.objects import Price, Quantity

        factory = OrderFactory(
            trader_id=self.node.trader_id,
            strategy_id=_strategy_id(),
            clock=self.node.kernel.clock,
            cache=self.node.cache,
            use_uuid_client_order_ids=True,
        )
        side = OrderSide.BUY if str(order.side).lower() in {"buy", "long"} else OrderSide.SELL
        qty = Quantity.from_str(_decimal_text(quantity))
        order_type = str(order.order_type or "market").strip().lower()
        if order_type == "market":
            if order.post_only:
                raise RuntimeError("market 订单不支持 post_only")
            if order.reduce_only and self.venue == "binance":
                raise RuntimeError("Binance Hedge Mode 不支持 reduce_only 参数，请使用明确反向仓位订单")
            nautilus_order = factory.market(
                instrument_id=instrument.id,
                order_side=side,
                quantity=qty,
                reduce_only=bool(order.reduce_only),
            )
        elif order_type == "limit":
            if order.price is None or Decimal(str(order.price)) <= 0:
                raise RuntimeError("limit 订单必须提供有效价格")
            if order.reduce_only and self.venue == "binance":
                raise RuntimeError("Binance Hedge Mode 不支持 reduce_only 参数，请使用明确反向仓位订单")
            tif = TimeInForce.GTX if order.post_only else TimeInForce.GTC
            nautilus_order = factory.limit(
                instrument_id=instrument.id,
                order_side=side,
                quantity=qty,
                price=Price.from_str(_decimal_text(Decimal(str(order.price)))),
                time_in_force=tif,
                reduce_only=bool(order.reduce_only),
                post_only=bool(order.post_only),
            )
        else:
            raise RuntimeError(f"Nautilus 暂不支持订单类型: {order.order_type}")
        command = SubmitOrder(
            trader_id=self.node.trader_id,
            strategy_id=_strategy_id(),
            order=nautilus_order,
            command_id=UUID4(),
            ts_init=_clock_timestamp_ns(self.node),
        )
        self.node.kernel.exec_engine.execute(command)
        return nautilus_order

    def _subscribe_quote_ticks(self, instrument_id: Any) -> None:
        """向 Nautilus DataEngine 订阅指定 instrument 的 quote ticks。"""
        key = str(instrument_id)
        if key in self._quote_subscriptions:
            return
        try:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.data.messages import SubscribeQuoteTicks
            from nautilus_trader.model.identifiers import Venue

            command = SubscribeQuoteTicks(
                instrument_id=instrument_id,
                client_id=None,
                venue=Venue(self.venue.upper()),
                command_id=UUID4(),
                ts_init=_clock_timestamp_ns(self.node),
            )
            self.node.kernel.data_engine.execute(command)
            self._quote_subscriptions.add(key)
            logger.info("Nautilus quote ticks 已订阅: venue={}, instrument={}", self.venue, instrument_id)
        except Exception as exc:
            logger.warning("Nautilus quote ticks 订阅失败: venue={}, instrument={}, error={}", self.venue, instrument_id, exc)

    def _subscribe_mark_prices(self, instrument_id: Any) -> None:
        """向 Nautilus DataEngine 订阅指定 instrument 的 mark prices。"""
        key = str(instrument_id)
        if key in self._mark_price_subscriptions:
            return
        try:
            from nautilus_trader.core.uuid import UUID4
            from nautilus_trader.data.messages import SubscribeMarkPrices
            from nautilus_trader.model.identifiers import Venue

            command = SubscribeMarkPrices(
                instrument_id=instrument_id,
                client_id=None,
                venue=Venue(self.venue.upper()),
                command_id=UUID4(),
                ts_init=_clock_timestamp_ns(self.node),
            )
            self.node.kernel.data_engine.execute(command)
            self._mark_price_subscriptions.add(key)
            logger.info("Nautilus mark prices 已订阅: venue={}, instrument={}", self.venue, instrument_id)
        except Exception as exc:
            logger.warning("Nautilus mark prices 订阅失败: venue={}, instrument={}, error={}", self.venue, instrument_id, exc)

    def _order_result(
        self,
        submitted_order: Any,
        real_quantity: Decimal,
        ledger_quantity: Decimal,
        mode: NautilusTradeMode,
    ) -> AdapterOrderResult:
        """从 Nautilus cache/fills report 汇总订单结果。"""
        client_order_id = str(getattr(submitted_order, "client_order_id", ""))
        cached = _cache_order(self.node, client_order_id)
        status = str(_field(cached or submitted_order, "status", "submitted")).lower()
        fills = self.get_trades(client_order_id)
        avg_price = _average_fill_price(fills)
        filled_qty = _fill_quantity(fills) or real_quantity
        return AdapterOrderResult(
            True,
            client_order_id,
            status or "submitted",
            float(ledger_quantity if mode == NautilusTradeMode.PAPER_PROBE else filled_qty),
            float(avg_price),
            0.0,
            f"Nautilus {mode.value} 订单已提交，真实订单 ID {client_order_id}",
        )

    def _run_node(self) -> None:
        """在后台线程运行 TradingNode。"""
        try:
            if self.node and not _node_is_running(self.node):
                self.node.run()
        except Exception as exc:
            self.status = NautilusRuntimeStatus.FAILED
            self.last_error = f"Nautilus venue {self.venue} runtime 运行失败: {exc}"
            logger.error(self.last_error)

    def _dispose_node(self) -> None:
        """释放 TradingNode 资源。"""
        node = self.node
        self.node = None
        if node and hasattr(node, "dispose"):
            try:
                node.dispose()
            except Exception as exc:
                logger.warning("Nautilus node dispose 失败: venue={}, error={}", self.venue, exc)

    def _fail(self, message: str) -> None:
        """记录失败状态并抛出异常。"""
        self.status = NautilusRuntimeStatus.FAILED
        self.last_error = message
        raise RuntimeError(message)


class NautilusRuntimeManager:
    """Nautilus TradingNode runtime 管理器。"""

    def __init__(self) -> None:
        self._runtimes: dict[NautilusRuntimeKey, NautilusVenueRuntime] = {}
        self._lock = RLock()

    def runtime_for(self, credential: ExchangeCredential) -> NautilusVenueRuntime:
        """获取或创建指定凭证对应的 runtime。"""
        key = NautilusRuntimeKey(
            venue=_venue(credential.venue),
            environment=str(credential.environment or "sandbox").strip().lower(),
            credential_id=getattr(credential, "id", None),
        )
        with self._lock:
            runtime = self._runtimes.get(key)
            if runtime is None:
                runtime = NautilusVenueRuntime(credential)
                runtime.ensure_loaded()
                self._runtimes[key] = runtime
            return runtime

    def reset(self) -> None:
        """停止并清空 runtime 缓存，测试或凭证刷新时使用。"""
        with self._lock:
            for runtime in self._runtimes.values():
                runtime.stop()
            self._runtimes.clear()

    def preload_enabled(self, db: Any) -> None:
        """启动时预加载已启用的非原生交易所 runtime。"""
        rows = (
            db.query(ExchangeCredential)
            .filter(
                ExchangeCredential.enabled.is_(True),
                ExchangeCredential.venue.notin_(["hyperliquid", "mt5"]),
            )
            .all()
        )
        for row in rows:
            self.runtime_for(row)


def _build_binance_node(row: ExchangeCredential, credentials: dict[str, Any]) -> NautilusNodeBuild:
    """构建 Binance USDT Futures TradingNode 配置。"""
    from nautilus_trader.adapters.binance.common.enums import BinanceAccountType, BinanceEnvironment
    from nautilus_trader.adapters.binance.config import BinanceDataClientConfig, BinanceExecClientConfig
    from nautilus_trader.adapters.binance.factories import BinanceLiveDataClientFactory, BinanceLiveExecClientFactory
    from nautilus_trader.common import Environment
    from nautilus_trader.common.config import InstrumentProviderConfig, LoggingConfig
    from nautilus_trader.live.config import LiveExecEngineConfig, TradingNodeConfig
    from nautilus_trader.model.identifiers import TraderId, Venue

    env = _binance_environment(row.environment)
    provider = InstrumentProviderConfig(load_all=True)
    data_config = BinanceDataClientConfig(
        venue=Venue("BINANCE"),
        # 行情和 instrument 加载使用公共连接，避免本机时间漂移导致私有签名请求阻塞行情启动。
        api_key=None,
        api_secret=None,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=env,
        instrument_provider=provider,
    )
    exec_config = BinanceExecClientConfig(
        venue=Venue("BINANCE"),
        api_key=str(credentials.get("api_key") or "") or None,
        api_secret=str(credentials.get("api_secret") or "") or None,
        account_type=BinanceAccountType.USDT_FUTURES,
        environment=env,
        instrument_provider=provider,
        use_reduce_only=False,
        recv_window_ms=30000,
    )
    config = TradingNodeConfig(
        environment=Environment.LIVE,
        trader_id=TraderId(f"CROSSHEDGE-{getattr(row, 'id', None) or '001'}"),
        logging=LoggingConfig(log_level="INFO"),
        exec_engine=LiveExecEngineConfig(
            snapshot_positions=True,
            snapshot_positions_interval_secs=30,
            position_check_interval_secs=30,
        ),
        data_clients={"BINANCE": data_config},
        exec_clients={"BINANCE": exec_config},
    )
    return NautilusNodeBuild(config, BinanceLiveDataClientFactory, BinanceLiveExecClientFactory)


SUPPORTED_NAUTILUS_VENUES: dict[str, NautilusVenueSpec] = {
    "binance": NautilusVenueSpec("binance", "BINANCE", _build_binance_node),
}
nautilus_runtime_manager = NautilusRuntimeManager()


def nautilus_live_supported(venue: str) -> bool:
    """判断 venue 是否已接入 Nautilus live runtime。"""
    return _venue(venue) in SUPPORTED_NAUTILUS_VENUES


def nautilus_probe_supported(venue: str) -> bool:
    """判断 venue 是否已接入 Nautilus paper_probe runtime。"""
    return _venue(venue) in SUPPORTED_NAUTILUS_VENUES


def _create_trading_node(config: Any) -> Any:
    """创建 TradingNode，测试可 monkeypatch 此函数。"""
    from nautilus_trader.live.node import TradingNode

    return TradingNode(config=config)


def _binance_environment(value: str) -> Any:
    """将项目环境字符串映射到 Binance Nautilus 环境。"""
    from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment

    normalized = str(value or "").strip().lower()
    if normalized in {"test", "testnet", "sandbox"}:
        return BinanceEnvironment.TESTNET
    if normalized == "demo":
        return BinanceEnvironment.DEMO
    return BinanceEnvironment.LIVE


def _strategy_id() -> Any:
    """返回手工下单使用的 Nautilus strategy ID。"""
    from nautilus_trader.model.identifiers import StrategyId

    return StrategyId("CROSSHEDGE-001")


def _clock_timestamp_ns(node: Any) -> int:
    """读取 Nautilus clock 当前纳秒时间。"""
    clock = getattr(getattr(node, "kernel", None), "clock", None)
    if clock and hasattr(clock, "timestamp_ns"):
        return int(clock.timestamp_ns())
    if clock and hasattr(clock, "timestamp"):
        return int(float(clock.timestamp()) * 1_000_000_000)
    import time

    return time.time_ns()


def _node_is_running(node: Any) -> bool:
    """兼容不同 fake/真实 node 的运行状态判断。"""
    attr = getattr(node, "is_running", None)
    return bool(attr() if callable(attr) else attr)


def _cache_account(node: Any, venue: str) -> Any:
    """从 cache 读取账户对象。"""
    cache = getattr(node, "cache", None)
    if cache is None:
        return None
    try:
        from nautilus_trader.model.identifiers import Venue

        account = cache.account_for_venue(Venue(venue.upper()))
        if account:
            return account
    except Exception:
        pass
    for method_name in ("accounts", "account_states"):
        method = getattr(cache, method_name, None)
        if callable(method):
            try:
                rows = method()
                for row in rows or []:
                    if venue.upper() in str(row).upper():
                        return row
            except Exception:
                continue
    return None


def _cache_positions(node: Any, venue: str) -> list[Any]:
    """从 cache 读取指定 venue 的持仓。"""
    cache = getattr(node, "cache", None)
    if cache is None:
        return []
    rows: list[Any] = []
    for method_name in ("positions_open", "positions"):
        method = getattr(cache, method_name, None)
        if not callable(method):
            continue
        try:
            current = method()
        except TypeError:
            try:
                from nautilus_trader.model.identifiers import Venue

                current = method(venue=Venue(venue.upper()))
            except Exception:
                current = []
        except Exception:
            current = []
        rows.extend(list(current or []))
    return [row for row in rows if venue.upper() in str(_field(row, "instrument_id", row)).upper()]


def _account_loaded(node: Any, venue: str) -> bool:
    """判断指定 venue 的账户状态是否已经进入 cache。"""
    return _cache_account(node, venue) is not None


def _cache_instruments(node: Any, venue: str) -> list[Any]:
    """从 cache 读取指定 venue 的 instruments。"""
    cache = getattr(node, "cache", None)
    method = getattr(cache, "instruments", None)
    if not callable(method):
        return []
    try:
        rows = method()
    except Exception:
        return []
    return [row for row in rows or [] if venue.upper() in str(getattr(row, "id", row)).upper()]


def _cache_quote(node: Any, instrument_id: Any) -> Any:
    """从 cache 读取 quote tick。"""
    cache = getattr(node, "cache", None)
    for name in ("quote_tick", "quote"):
        method = getattr(cache, name, None)
        if callable(method):
            try:
                return method(instrument_id)
            except Exception:
                continue
    return None


def _cache_mark_price(node: Any, instrument_id: Any) -> Any:
    """从 cache 读取 mark price update。"""
    cache = getattr(node, "cache", None)
    method = getattr(cache, "mark_price", None)
    if callable(method):
        try:
            return method(instrument_id)
        except Exception:
            return None
    return None


def _cache_order_book(node: Any, instrument_id: Any) -> Any:
    """从 cache 读取订单簿。"""
    cache = getattr(node, "cache", None)
    for name in ("order_book", "book"):
        method = getattr(cache, name, None)
        if callable(method):
            try:
                return method(instrument_id)
            except Exception:
                continue
    return None


def _cache_order(node: Any, order_id: str) -> Any:
    """从 cache 读取订单。"""
    cache = getattr(node, "cache", None)
    for name in ("order", "order_by_client_order_id"):
        method = getattr(cache, name, None)
        if callable(method):
            try:
                order = method(order_id)
                if order:
                    return order
            except Exception:
                continue
    method = getattr(cache, "orders", None)
    if callable(method):
        try:
            for order in method() or []:
                if order_id in str(order):
                    return order
        except Exception:
            pass
    return None


def _safe_report(trader: Any, method_name: str) -> list[Any]:
    """安全调用 Nautilus trader report。"""
    method = getattr(trader, method_name, None)
    if not callable(method):
        return []
    try:
        result = method()
        if hasattr(result, "to_dict"):
            return list(result.to_dict("records"))
        return list(result or [])
    except Exception:
        return []


def _instrument_specs(instrument: Any) -> NautilusInstrumentSpec:
    """从 Nautilus instrument 提取数量/价格规格。"""
    step = _decimal(
        _field(
            instrument,
            "size_increment",
            _field(instrument, "lot_size", _field(instrument, "quantity_increment", 0)),
        )
    )
    min_qty = _decimal(_field(instrument, "min_quantity", _field(instrument, "min_qty", step)))
    min_notional = _decimal(_field(instrument, "min_notional", _field(instrument, "min_notional_value", 0)))
    price_increment = _decimal(_field(instrument, "price_increment", _field(instrument, "tick_size", 0)))
    return NautilusInstrumentSpec(
        step=step if step > 0 else Decimal("0"),
        min_qty=min_qty if min_qty > 0 else step,
        min_notional=min_notional,
        price_increment=price_increment,
    )


def _probe_quantity(specs: NautilusInstrumentSpec, reference_price: Decimal, configured_min_base_size: float) -> Decimal:
    """计算 paper_probe 的真实最小成交量。"""
    min_qty = max(specs.min_qty, _decimal(configured_min_base_size), specs.step)
    if reference_price > 0 and specs.min_notional > 0:
        min_qty = max(min_qty, specs.min_notional / reference_price)
    quantity = _ceil_to_step(min_qty, specs.step)
    if quantity <= 0:
        raise RuntimeError("Nautilus paper_probe 最小订单数量无法计算")
    return quantity


def _live_quantity(specs: NautilusInstrumentSpec, requested: Decimal, reference_price: Decimal) -> Decimal:
    """校验并规整 live 真实订单数量。"""
    if requested <= 0:
        raise RuntimeError("Nautilus live 下单数量必须大于 0")
    quantity = _floor_to_step(requested, specs.step)
    if quantity <= 0:
        raise RuntimeError("Nautilus live 下单数量无法计算")
    if specs.min_qty > 0 and quantity < specs.min_qty:
        raise RuntimeError(f"Nautilus live 下单数量低于最小数量: {quantity} < {specs.min_qty}")
    if reference_price > 0 and specs.min_notional > 0 and quantity * reference_price < specs.min_notional:
        raise RuntimeError(f"Nautilus live 下单名义价值低于最小名义额: {quantity * reference_price} < {specs.min_notional}")
    return quantity


def _reject_unsupported_order_flags(order: AdapterOrder) -> None:
    """拒绝当前统一接口尚未安全映射到 Nautilus 的参数。"""
    if order.ttl_seconds:
        raise RuntimeError("Nautilus live 当前不支持 ttl_seconds，请使用 GTC/GTX 订单")


def _reference_price(ticker: dict[str, float], side: str) -> Decimal:
    """按买卖方向选择名义价值参考价。"""
    key = "ask" if str(side).lower() in {"buy", "long"} else "bid"
    return _decimal(ticker.get(key) or 0)


def _average_fill_price(fills: list[dict[str, Any]]) -> Decimal:
    """根据 fills report 计算成交均价。"""
    qty_sum = Decimal("0")
    notional_sum = Decimal("0")
    for fill in fills:
        qty = abs(_decimal(fill.get("quantity") or fill.get("last_qty") or fill.get("filled_qty")))
        price = _decimal(fill.get("price") or fill.get("last_px") or fill.get("avg_px"))
        if qty > 0 and price > 0:
            qty_sum += qty
            notional_sum += qty * price
    return notional_sum / qty_sum if qty_sum > 0 else Decimal("0")


def _fill_quantity(fills: list[dict[str, Any]]) -> Decimal:
    """汇总 fills report 成交量。"""
    total = Decimal("0")
    for fill in fills:
        total += abs(_decimal(fill.get("quantity") or fill.get("last_qty") or fill.get("filled_qty")))
    return total


def _quote_from_book(book: Any) -> Any:
    """从订单簿对象提取顶层报价。"""
    if book is None:
        return None
    try:
        bid = book.best_bid()
        ask = book.best_ask()
        return {
            "bid": _field(bid, "price", 0),
            "ask": _field(ask, "price", 0),
            "bid_size": _field(bid, "size", 0),
            "ask_size": _field(ask, "size", 0),
        }
    except Exception:
        return None


def _mark_price_value(mark_price: Any) -> float:
    """从 Nautilus MarkPriceUpdate 提取 mark 价格。"""
    if mark_price is None:
        return 0.0
    return _float(_field(mark_price, "mark", _field(mark_price, "price", 0)))


def _order_book_to_dict(symbol: str, book: Any, depth: int) -> dict[str, Any]:
    """将 Nautilus order book 转成项目统一字典。"""
    if book is None:
        return {"symbol": symbol, "depth": depth, "bids": [], "asks": []}
    bids = _book_side(book, "bids", depth)
    asks = _book_side(book, "asks", depth)
    return {"symbol": symbol, "depth": depth, "bids": bids, "asks": asks}


def _book_side(book: Any, name: str, depth: int) -> list[list[float]]:
    """读取订单簿一侧深度。"""
    side = getattr(book, name, None)
    if callable(side):
        try:
            rows = side(depth)
        except TypeError:
            rows = side()
    else:
        rows = side or []
    result = []
    for row in list(rows or [])[:depth]:
        result.append([_float(_field(row, "price", row[0] if isinstance(row, (list, tuple)) else 0)), _float(_field(row, "size", row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else 0))])
    return result


def _position_to_dict(venue: str, item: Any) -> dict[str, Any]:
    """将 Nautilus position/report 转为项目持仓字典。"""
    data = _object_to_dict(item)
    qty = _position_quantity(data)
    entry_price = _decimal(data.get("avg_px_open") or data.get("entry_price") or data.get("avg_price"))
    mark_price = _decimal(data.get("mark_price") or data.get("last_price") or entry_price)
    return {
        "platform": venue,
        "symbol": str(data.get("instrument_id") or data.get("symbol") or ""),
        "side": "long" if qty > 0 else "short",
        "quantity": float(abs(qty)),
        "entry_price": float(entry_price),
        "mark_price": float(mark_price),
        "unrealized_pnl": _float(data.get("unrealized_pnl") or data.get("unrealized_return") or 0),
        "margin_used": _float(data.get("margin") or data.get("initial_margin") or 0),
        "liquidation_price": _optional_float(data.get("liquidation_price")),
    }


def _position_quantity(item: Any) -> Decimal:
    """读取 position 数量，兼容对象和 report dict。"""
    data = _object_to_dict(item)
    signed = data.get("signed_decimal_qty") or data.get("signed_qty") or data.get("net_position")
    if signed is not None:
        return _decimal(signed)
    qty = _decimal(data.get("quantity") or data.get("size"))
    side = str(data.get("position_side") or data.get("side") or "").lower()
    if "short" in side:
        return -abs(qty)
    if "flat" in side:
        return Decimal("0")
    return qty


def _dedupe_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按平台、品种和方向去重 Nautilus cache/report 的重复持仓。"""
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("platform") or ""),
            str(row.get("symbol") or ""),
            str(row.get("side") or ""),
        )
        deduped[key] = row
    return list(deduped.values())


def _account_to_dict(account: Any) -> dict[str, Any]:
    """将 Nautilus Account 转为项目账户字段。"""
    currency = _account_currency(account)
    total = _account_amount(account, "balance_total", currency)
    free = _account_amount(account, "balance_free", currency)
    locked = _account_amount(account, "balance_locked", currency)
    margin_init = _account_margin_amount(account, "margin_init_for_currency", "total_margin_init", "account_margins_init", currency)
    margin_maint = _account_margin_amount(account, "margin_maint_for_currency", "total_margin_maint", "account_margins_maint", currency)
    data = _object_to_dict(account)
    data.update(
        {
            "currency": str(currency),
            "balance_total": total,
            "balance_free": free,
            "balance_locked": locked,
            "margin_init": margin_init,
            "margin_maint": margin_maint,
            "equity": total,
            "available": free,
            "margin_used": margin_init,
        }
    )
    return data


def _account_currency(account: Any) -> Any:
    """选择账户展示币种，优先使用 USDT。"""
    try:
        from nautilus_trader.model.currencies import USDT

        return USDT
    except Exception:
        return "USDT"


def _account_amount(account: Any, method_name: str, currency: Any) -> float:
    """调用 Nautilus Account 金额方法并转换为 float。"""
    method = getattr(account, method_name, None)
    if not callable(method):
        return 0.0
    try:
        value = method(currency)
    except TypeError:
        try:
            value = method()
        except Exception:
            return 0.0
    except Exception:
        return 0.0
    return _float_money(value)


def _account_margin_amount(account: Any, currency_method: str, total_method: str, fallback_method: str, currency: Any) -> float:
    """读取 Nautilus MarginAccount 的保证金金额。"""
    for method_name in (currency_method, total_method):
        amount = _account_amount(account, method_name, currency)
        if amount:
            return amount
    method = getattr(account, fallback_method, None)
    if callable(method):
        try:
            rows = method()
        except Exception:
            rows = []
        total = 0.0
        values = rows.values() if isinstance(rows, dict) else rows
        for row in values or []:
            total += _float_money(row)
        if total:
            return total
    return 0.0


def _object_to_dict(value: Any) -> dict[str, Any]:
    """将 Nautilus 对象安全转换为 dict。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:
            pass
    fields = getattr(value, "__struct_fields__", None) or getattr(value, "__dataclass_fields__", None)
    if fields:
        names = fields.keys() if isinstance(fields, dict) else fields
        return {str(name): getattr(value, str(name), None) for name in names}
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:
            continue
        if callable(attr):
            continue
        if isinstance(attr, (str, int, float, bool, Decimal, type(None))):
            result[name] = attr
    return result


def _row_to_dict(row: Any) -> dict[str, Any]:
    """将 pandas/report row 转成 dict。"""
    if isinstance(row, dict):
        return row
    if hasattr(row, "to_dict"):
        try:
            return row.to_dict()
        except Exception:
            pass
    return _object_to_dict(row)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """从 dict 或对象中读取字段。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _decimal(value: Any) -> Decimal:
    """安全转换 Decimal。"""
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _float(value: Any) -> float:
    """安全转换 float。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _float_money(value: Any) -> float:
    """解析 Nautilus Money / Decimal / 字符串金额。"""
    amount = getattr(value, "as_double", None)
    if callable(amount):
        try:
            return float(amount())
        except Exception:
            pass
    for attr in ("amount", "raw"):
        if hasattr(value, attr):
            parsed = _float(getattr(value, attr))
            if parsed:
                return parsed
    text = str(value or "0").strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    return _float(text)


def _optional_float(value: Any) -> float | None:
    """安全转换可选 float。"""
    parsed = _float(value)
    return parsed if parsed > 0 else None


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向上取整到步长。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取整到步长，避免 live 真实下单超过策略数量。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _decimal_text(value: Decimal) -> str:
    """Decimal 转交易所可读字符串。"""
    return format(value.normalize(), "f")


def _normalize_symbol(symbol: str) -> str:
    """标准化品种名用于模糊匹配。"""
    return str(symbol or "").upper().replace("/", "").replace("-", "").replace("_", "").replace(":", "")


def _reject(message: str) -> AdapterOrderResult:
    """构造拒单结果。"""
    return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, message)


def _venue(value: str) -> str:
    """标准化 venue 名称。"""
    return str(value or "").strip().lower()


def _nautilus_import_error() -> str:
    """检查 NautilusTrader 是否可导入。"""
    try:
        import nautilus_trader  # noqa: F401
    except Exception as exc:
        return str(exc)
    return ""
