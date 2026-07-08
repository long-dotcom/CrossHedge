"""
NautilusTrader 交易所适配器
===========================

除 MT5 / Hyperliquid 外，其他交易所统一通过本适配器接入 NautilusTrader。
适配器本身只做薄封装，实际行情、账户、持仓和订单能力由
``NautilusRuntimeManager`` 维护。
"""

from __future__ import annotations

from typing import Any

from app.adapters.base import Account, AdapterOrder, AdapterOrderResult, Ticker
from app.adapters.nautilus_runtime import NautilusTradeMode, nautilus_runtime_manager
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ExchangeCredential, SymbolMapping
from app.db.session import SessionLocal

logger = get_logger(__name__)


class NautilusAdapter:
    """NautilusTrader 统一适配器。

    非原生交易所有两种交易模式：
    - ``paper_probe``：真实最小订单取成交价，paper 账本使用策略目标数量。
    - ``live``：真实订单按策略目标数量提交。

    参数:
        venue: 交易所名称（如 ``"binance"`` / ``"okx"`` / ``"bybit"``）。
        live: 是否启用真实交易路径。
    """

    def __init__(self, venue: str, *, live: bool = False) -> None:
        self.platform = venue.strip().lower()
        self.live = live
        # 是否启用 paper_probe：真实最小订单取价，paper 账本数量不变。
        self.paper_price_probe = False
        self.settings = get_settings()

    def _runtime(self):
        """获取当前 venue 对应的 Nautilus runtime。"""
        with SessionLocal() as db:
            try:
                credential = self._credential(db)
            except Exception as exc:
                raise RuntimeError(f"读取 {self.platform} 交易所配置失败: {exc}") from exc
            if credential is None:
                raise RuntimeError(f"缺少已启用的 {self.platform} 交易所配置")
            return nautilus_runtime_manager.runtime_for(credential)

    def get_symbols(self) -> list[str]:
        """返回支持的品种列表。"""
        return []

    def get_account(self) -> Account:
        """获取账户权益摘要。

        NautilusTrader 不可用时返回零值账户。
        """
        try:
            account = self._runtime().get_account()
        except RuntimeError as exc:
            logger.warning("Nautilus 账户读取失败: venue={}, error={}", self.platform, exc)
            return Account(self.platform, 0.0, 0.0, 0.0, 1.0, currency="USD")

        equity = _number(
            account,
            "totalMarginBalance",
            "totalWalletBalance",
            "balance_total",
            "total",
            "equity",
        )
        available = _number(account, "availableBalance", "free", "balance_free", "available", "cash")
        margin_used = _number(account, "totalInitialMargin", "margin_used", "margin_init", "initial_margin", "margin")
        currency = str(account.get("currency") or ("USDT" if self.platform == "binance" else "USD"))
        return Account(
            self.platform,
            equity,
            available,
            margin_used,
            (equity / margin_used) if margin_used > 0 else 1.0,
            currency=currency,
        )

    def get_positions(self) -> list[dict[str, Any]]:
        """获取当前持仓列表。"""
        return self._runtime().get_positions()

    def get_ticker(self, symbol: str) -> Ticker:
        """获取指定品种的实时行情。"""
        ticker = self._runtime().get_ticker(symbol)
        return Ticker(
            symbol=symbol,
            bid=ticker["bid"],
            ask=ticker["ask"],
            depth_notional=ticker["depth_notional"],
            timestamp=utc_now(),
        )

    def get_mark_price(self, symbol: str) -> float:
        """获取指定品种的 Nautilus 标记价格。"""
        return self._runtime().get_mark_price(symbol)

    def get_orderbook(self, symbol: str, depth: int = 5) -> dict[str, Any]:
        """获取订单簿。"""
        return self._runtime().get_orderbook(symbol, depth)

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        """下单请求。

        ``paper_price_probe=True`` 时走 paper_probe；否则 ``live=True`` 时走真实下单。
        """
        if self.paper_price_probe:
            try:
                return self._runtime().place_order(
                    order,
                    NautilusTradeMode.PAPER_PROBE,
                    configured_min_base_size=self._configured_min_base_size(order),
                )
            except RuntimeError as exc:
                return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, str(exc))

        if self.live:
            try:
                return self._runtime().place_order(order, NautilusTradeMode.LIVE)
            except RuntimeError as exc:
                return AdapterOrderResult(False, "", "rejected", 0.0, 0.0, 0.0, str(exc))
        return AdapterOrderResult(
            False, "", "rejected", 0.0, 0.0, 0.0,
            "Nautilus 适配器仅支持 paper_probe 或 live 真实交易模式",
        )

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单。"""
        return self._runtime().cancel_order(order_id)

    def get_order(self, order_id: str) -> dict[str, Any]:
        """查询订单。"""
        return self._runtime().get_order(order_id)

    def get_trades(self, order_id: str) -> list[dict[str, Any]]:
        """查询成交明细。"""
        return self._runtime().get_trades(order_id)

    def _credential(self, db) -> ExchangeCredential | None:
        """从数据库获取当前 venue 的已启用凭证。"""
        return (
            db.query(ExchangeCredential)
            .filter(
                ExchangeCredential.venue == self.platform,
                ExchangeCredential.enabled.is_(True),
            )
            .first()
        )

    def _configured_min_base_size(self, order: AdapterOrder) -> float:
        """从品种映射查找当前 venue 的最小基础下单量。"""
        symbols = {
            str(order.symbol or "").strip().upper(),
            str(order.venue_symbol or "").strip().upper(),
        }
        with SessionLocal() as db:
            rows = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
            for row in rows:
                if _venue(row.leg_a_venue) == self.platform:
                    candidates = {row.symbol, row.leg_a_symbol, row.leg_a_venue_symbol}
                    if symbols & {str(item or "").strip().upper() for item in candidates}:
                        return float(row.leg_a_min_base_size or row.min_order_size or 0.0)
                if _venue(row.leg_b_venue) == self.platform:
                    candidates = {row.symbol, row.leg_b_symbol}
                    if symbols & {str(item or "").strip().upper() for item in candidates}:
                        return float(row.min_order_size or 0.0)
        return 0.0


def nautilus_account_snapshot(venue: str) -> dict[str, Any]:
    """获取 Nautilus 只读适配器的账户快照字典。

    用于 API 接口返回账户信息，包含 portfolio_value、withdrawable 等衍生字段。

    参数:
        venue: 交易所名称。

    返回:
        包含账户权益各字段的字典。
    """
    adapter = NautilusAdapter(venue)
    account = adapter.get_account()
    return {
        "platform": account.platform,
        "equity": account.equity,
        "available_balance": account.available_balance,
        "margin_used": account.margin_used,
        "margin_ratio": account.margin_ratio,
        "currency": account.currency,
        "portfolio_value": account.equity,
        "perp_equity": account.equity,
        "withdrawable": account.available_balance,
        "free_collateral": account.available_balance,
        "data_source": "nautilus_runtime",
        "created_at": utc_now(),
    }


NautilusReadOnlyAdapter = NautilusAdapter


def _venue(value: str) -> str:
    """标准化 venue 名称。"""
    return str(value or "").strip().lower()


def _number(data: dict[str, Any], *keys: str) -> float:
    """从 Nautilus account dict 中按多个候选字段读取数值。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0
