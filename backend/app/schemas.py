"""
Pydantic 请求/响应模型
======================

从源项目 schemas.py 完整迁移，为所有 API 端点的请求体和响应体
提供类型校验与文档生成支持。

使用方式::

    from app.schemas import LoginRequest, TokenResponse
"""

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 认证相关
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    """管理员登录请求体。"""
    username: str
    password: str


class TokenResponse(BaseModel):
    """登录成功后返回的 Token 响应。"""
    access_token: str
    token_type: str = "bearer"
    user: dict[str, Any]


# ---------------------------------------------------------------------------
# 策略设置
# ---------------------------------------------------------------------------

class StrategySettingsIn(BaseModel):
    """策略参数全量更新请求体。"""
    min_net_profit: float
    min_annualized_return: float
    signal_mode: str = "statistical"
    statistical_lookback_range: str = "1h"
    statistical_min_samples: int = 200
    reachable_entry_percentile: float = 0.75
    reachable_entry_zscore: float = 1.0
    cost_guard_percentile: float = 0.90
    min_unit_edge: float = 0.0
    min_total_profit: float = 0.5
    auto_close_enabled: bool = True
    auto_close_live_enabled: bool = False
    exit_target_percentile: float = 0.25
    auto_close_unit_profit_buffer: float = 0.0
    auto_close_min_profit: float = 0.0
    default_notional: float
    max_holding_minutes: int
    execution_mode: str
    paper_use_live_account_risk: bool = False
    auto_execute_enabled: bool = False
    auto_execute_paper_only: bool = True
    auto_execute_min_hold_ms: int = 300
    auto_execute_confirm_ticks: int = 2
    auto_execute_cooldown_seconds: int = 30
    auto_execute_max_per_symbol_open_groups: int = 1
    auto_execute_max_global_open_groups: int = 3
    auto_execute_min_net_profit: float = 0.0
    paper_decision_delay_ms_min: int = 50
    paper_decision_delay_ms_max: int = 200
    paper_leg_a_latency_ms_min: int = 80
    paper_leg_a_latency_ms_max: int = 200
    paper_leg_b_latency_ms_min: int = 120
    paper_leg_b_latency_ms_max: int = 350
    cb_cooldown_seconds: float = 3.0
    cb_initial_threshold: float = 0.75
    cb_baseline_multiplier: float = 2.0
    cb_min_baseline_samples: int = 50
    cb_detection_seconds: float = 5.0


# ---------------------------------------------------------------------------
# 风控设置
# ---------------------------------------------------------------------------

class RiskSettingsIn(BaseModel):
    """风控参数全量更新请求体。"""
    mode: str
    max_order_notional: float
    max_symbol_exposure: float
    max_total_leverage: float
    max_new_margin_fraction: float
    new_order_leverage: float
    min_margin_ratio: float
    max_slippage_bps: float
    max_market_age_seconds: int
    max_api_errors: int
    max_total_open_notional: float = 10000.0
    max_global_open_groups: int = 3
    max_pending_open_groups: int = 2
    max_daily_loss: float = 0.0

    @model_validator(mode="after")
    def validate_capacity_limits(self) -> "RiskSettingsIn":
        if self.max_order_notional <= 0 or self.max_total_open_notional <= 0:
            raise ValueError("单笔和全局累计名义金额上限必须大于 0")
        if self.max_global_open_groups < 1 or self.max_pending_open_groups < 1:
            raise ValueError("全局未平和在途开仓组上限必须至少为 1")
        if self.max_pending_open_groups > self.max_global_open_groups:
            raise ValueError("在途开仓组上限不能大于全局未平对冲组上限")
        if self.max_daily_loss < 0:
            raise ValueError("单日亏损上限不能为负数")
        return self


class RiskModeIn(BaseModel):
    """切换风控模式请求体。"""
    mode: str


# ---------------------------------------------------------------------------
# 实盘交易 / 执行设置
# ---------------------------------------------------------------------------

class LiveTradingIn(BaseModel):
    """实盘交易开关请求体。"""
    enabled: bool
    confirmation: str = ""


class ExecutionSettingsIn(BaseModel):
    """执行参数更新请求体。"""
    paper_live_probe_enabled: bool = False
    paper_probe_max_notional: float = Field(default=20.0, ge=10.0, le=1000.0)
    paper_probe_daily_max_runs: int = Field(default=200, ge=1, le=100000)
    paper_probe_daily_max_notional: float = Field(default=4000.0, ge=10.0, le=10000000.0)
    paper_probe_cooldown_ms: int = Field(default=500, ge=0, le=60000)
    paper_probe_flatten_timeout_seconds: float = Field(default=20.0, ge=3.0, le=300.0)
    paper_probe_maker_timeout_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    confirmation: str = ""


# ---------------------------------------------------------------------------
# 交易所凭据
# ---------------------------------------------------------------------------

class ExchangeCredentialIn(BaseModel):
    """交易所 API 凭据创建/更新请求体。"""
    venue: str
    display_name: str = ""
    environment: str = "live"
    enabled: bool = False
    read_only: bool = True
    credentials: dict[str, Any] = {}

    @field_validator("venue", "display_name", "environment")
    @classmethod
    def strip_exchange_text(cls, value: str) -> str:
        return value.strip()


# ---------------------------------------------------------------------------
# 对冲组操作
# ---------------------------------------------------------------------------

class CloseHedgeGroupIn(BaseModel):
    """关闭对冲组请求体。"""
    reason: str = "manual"
    force: bool = False


class RecoverHedgeGroupIn(BaseModel):
    """异常对冲组恢复请求；confirmation 防止误触真实回平。"""
    reason: str = "manual recovery flatten"
    confirmation: str


class VoidHedgeGroupIn(BaseModel):
    """无真实敞口异常组的作废归档请求。"""
    reason: str = "人工作废异常对冲组"
    confirmation: str


class AdoptPositionIn(BaseModel):
    """接管外部仓位请求体。"""
    reason: str = "adopt external live position"
    symbol: str = ""


# ---------------------------------------------------------------------------
# Venue 探针测试
# ---------------------------------------------------------------------------

class VenueProbeTestIn(BaseModel):
    """Venue 探针测试请求体。"""
    symbol: str
    venue: str = ""
    side: str = "buy"
    quantity: float | None = None
    reduce_only: bool = False
    submit: bool = False
    slippage: float | None = None
    confirmation: str = ""

    @field_validator("symbol", "venue", "side", "confirmation")
    @classmethod
    def strip_probe_text(cls, value: str) -> str:
        return value.strip()


class ReplayExecutionIntentIn(BaseModel):
    """从不可变事件重建执行投影的显式确认请求。"""

    confirmation: str

    @field_validator("confirmation")
    @classmethod
    def strip_replay_confirmation(cls, value: str) -> str:
        return value.strip()


# ---------------------------------------------------------------------------
# 品种映射
# ---------------------------------------------------------------------------

class SymbolMappingIn(BaseModel):
    """品种映射创建/更新请求体。"""
    symbol: str
    leg_a_venue_symbol: str
    mt5_symbol: str
    leg_a_venue: str
    leg_a_symbol: str = ""
    leg_b_venue: str = "mt5"
    leg_b_symbol: str = ""
    base_asset: str = ""
    quote_asset: str = "USD"
    contract_multiplier: float = 1.0
    min_order_size: float = 0.001
    min_entry_spread: float = 0.0
    max_close_spread: float = 0.0
    mt5_min_lot: float = 0.0
    mt5_volume_step: float = 0.0
    mt5_contract_size: float = 1.0
    mt5_currency_base: str = ""
    mt5_currency_profit: str = "USD"
    mt5_currency_margin: str = "USD"
    mt5_calc_mode: int = 0
    mt5_min_base_size: float = 0.0
    leg_a_min_base_size: float = 0.0
    leg_a_min_notional: float = 10.0
    target_notional: float = 1000.0
    max_open_notional: float = 5000.0
    max_open_groups: int = 1
    open_cooldown_seconds: int = 30
    max_daily_opens: int = 0
    max_daily_open_notional: float = 0.0
    allow_opposite_direction: bool = False
    max_holding_minutes: int = 240
    execution_style: str = "simultaneous_market"
    maker_leg: str = "a"
    maker_offset_bps: float = 1.0
    maker_order_ttl_seconds: int = 3
    maker_unfilled_action: str = "cancel"
    leg_a_close_order_type: str = "market"
    leg_b_close_order_type: str = "market"
    hl_open_order_type: str = "market"
    hl_close_order_type: str = "market"
    hl_post_only: bool = False
    hl_maker_offset_bps: float = 1.0
    hl_order_ttl_seconds: int = 3
    hl_unfilled_action: str = "cancel"
    single_leg_action: str = "manual_intervention"
    mt5_open_order_type: str = "market"
    mt5_close_order_type: str = "market"
    mt5_session_enabled: bool = True
    mt5_session_auto_sync: bool = True
    mt5_session_template: str = "auto"
    mt5_session_timezone: str = "UTC"
    mt5_regular_sessions_json: str = "[]"
    mt5_close_only_sessions_json: str = "[]"
    mt5_quote_only_sessions_json: str = "[]"
    mt5_session_source: str = "manual"
    mt5_pre_close_no_open_minutes: int = 15
    mt5_post_open_cooldown_minutes: int = 10
    allow_hold_through_mt5_close: bool = False
    quantity_precision: int = 4
    price_precision: int = 2
    min_tick: float = 0.01
    max_slippage_bps: float = 8.0
    enabled: bool = True

    @field_validator(
        "symbol", "leg_a_venue_symbol", "mt5_symbol",
        "leg_a_venue", "leg_a_symbol", "leg_b_venue", "leg_b_symbol",
        "base_asset", "quote_asset",
        "mt5_currency_base", "mt5_currency_profit", "mt5_currency_margin",
        "mt5_session_template", "mt5_session_timezone", "mt5_session_source",
    )
    @classmethod
    def strip_symbol_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def normalize_legs_and_validate_leg_a_venue_symbol(self) -> "SymbolMappingIn":
        """规范化双腿 venue 并校验 Hyperliquid symbol 格式。"""
        self.leg_a_venue = (self.leg_a_venue or "").strip().lower()
        self.leg_b_venue = (self.leg_b_venue or "mt5").strip().lower()
        supported_venues = {"hyperliquid", "mt5", "binance"}
        unsupported = {self.leg_a_venue, self.leg_b_venue} - supported_venues
        if unsupported:
            raise ValueError(f"当前仅支持交易场所: {', '.join(sorted(supported_venues))}")
        self.leg_a_symbol = (self.leg_a_symbol or self.leg_a_venue_symbol).strip()
        self.leg_b_symbol = (self.leg_b_symbol or self.mt5_symbol).strip()
        if self.leg_a_venue == self.leg_b_venue:
            raise ValueError("两条腿不能选择同一个 venue")
        if not self.leg_a_symbol or not self.leg_b_symbol:
            raise ValueError("两条腿都必须填写 venue symbol")
        if not self.leg_a_venue_symbol.strip():
            raise ValueError("leg_a_venue_symbol 不能为空")
        # 历史字段名仍保留在数据库中，但语义统一为 Leg A / Leg B。
        # 执行策略是最高层选择，保存时同步订单类型，避免成本估算与真实下单方式脱节。
        legacy_styles = {
            "taker_taker": "simultaneous_market",
            "hyper_maker_mt5_taker": "maker_then_market",
        }
        self.execution_style = legacy_styles.get(self.execution_style, self.execution_style)
        if self.execution_style not in {"simultaneous_market", "maker_then_market"}:
            raise ValueError("执行模式必须是 simultaneous_market 或 maker_then_market")
        self.maker_leg = (self.maker_leg or "a").strip().lower()
        if self.maker_leg not in {"a", "b"}:
            raise ValueError("Maker 腿必须是 a 或 b")
        maker_venue = self.leg_a_venue if self.maker_leg == "a" else self.leg_b_venue
        if self.execution_style == "maker_then_market" and maker_venue == "mt5":
            raise ValueError("MT5 当前仅支持市价单，不能选作 Maker 腿")
        if self.maker_unfilled_action not in {"cancel", "market_fallback"}:
            raise ValueError("Maker 未成交动作必须是 cancel 或 market_fallback")
        if self.execution_style == "maker_then_market" and self.maker_order_ttl_seconds < 1:
            raise ValueError("Maker 挂单 TTL 必须至少为 1 秒")
        if self.maker_offset_bps < 0:
            raise ValueError("Maker 挂单偏移不能为负数")
        if self.target_notional <= 0:
            raise ValueError("单次目标名义金额必须大于 0")
        if self.max_open_notional < self.target_notional:
            raise ValueError("单品种累计名义金额上限不能小于单次目标名义金额")
        if self.max_open_groups < 1:
            raise ValueError("单品种未平对冲组上限必须至少为 1")
        if self.open_cooldown_seconds < 0 or self.max_daily_opens < 0 or self.max_daily_open_notional < 0:
            raise ValueError("开仓冷却和每日限额不能为负数")
        if self.max_holding_minutes < 1:
            raise ValueError("最大持仓时间必须至少为 1 分钟")
        if self.execution_style == "simultaneous_market":
            self.hl_open_order_type = "market"
            self.mt5_open_order_type = "market"
            self.hl_post_only = False
        elif self.execution_style == "maker_then_market":
            self.hl_open_order_type = "limit" if self.maker_leg == "a" else "market"
            self.mt5_open_order_type = "limit" if self.maker_leg == "b" else "market"
            self.hl_post_only = True
        # 同步历史字段，保证旧版本进程滚动升级期间仍能读取一致配置。
        self.hl_maker_offset_bps = self.maker_offset_bps
        self.hl_order_ttl_seconds = self.maker_order_ttl_seconds
        self.hl_unfilled_action = "taker_fallback" if self.maker_unfilled_action == "market_fallback" else "cancel"
        # 平仓跟随开仓执行模式；Maker 平仓价格由执行时实时盘口生成。
        self.leg_a_close_order_type = self.hl_open_order_type
        self.leg_b_close_order_type = self.mt5_open_order_type
        self.hl_close_order_type = self.leg_a_close_order_type
        self.mt5_close_order_type = self.leg_b_close_order_type
        if self.leg_a_venue == "hyperliquid" and not re.match(
            r'^[A-Z][A-Z0-9]*(-[A-Z0-9]+)?$', self.leg_a_venue_symbol
        ) and ":" not in self.leg_a_venue_symbol:
            raise ValueError(
                "Hyperliquid 标准永续合约仅接受大写字母开头的 symbol"
                "（如 BTC, ETH-USDT）或 HIP3 格式（如 xyz:JP225）"
            )
        return self


# ---------------------------------------------------------------------------
# 通用 ORM 响应基类
# ---------------------------------------------------------------------------

class ORMModel(BaseModel):
    """支持 from_attributes 的 Pydantic 基类。"""
    model_config = ConfigDict(from_attributes=True)


class RowResponse(ORMModel):
    """带 id + created_at 的通用行响应。"""
    id: int
    created_at: datetime
