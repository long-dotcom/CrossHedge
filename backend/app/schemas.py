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

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


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
    paper_live_parallel_execution: bool = True
    confirmation: str = ""


# ---------------------------------------------------------------------------
# 交易所凭据
# ---------------------------------------------------------------------------

class ExchangeCredentialIn(BaseModel):
    """交易所 API 凭据创建/更新请求体。"""
    venue: str
    display_name: str = ""
    environment: str = "sandbox"
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
    execution_style: str = "taker_taker"
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
        self.leg_a_symbol = (self.leg_a_symbol or self.leg_a_venue_symbol).strip()
        self.leg_b_symbol = (self.leg_b_symbol or self.mt5_symbol).strip()
        if self.leg_a_venue == self.leg_b_venue:
            raise ValueError("两条腿不能选择同一个 venue")
        if not self.leg_a_symbol or not self.leg_b_symbol:
            raise ValueError("两条腿都必须填写 venue symbol")
        if not self.leg_a_venue_symbol.strip():
            raise ValueError("leg_a_venue_symbol 不能为空")
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
