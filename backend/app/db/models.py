"""数据库 ORM 模型定义模块。

本模块定义了 CrossHedge 系统所有数据库表的 ORM 模型，涵盖：
- 用户与权限（User）
- 审计日志（AuditLog）
- 系统配置（SystemSetting）
- 交易所凭据（ExchangeCredential）
- 策略参数（StrategySetting）
- 风控参数（RiskSetting）
- 品种映射（SymbolMapping）
- 账户快照（AccountSnapshot）
- 持仓（Position）
- 行情快照（MarketSnapshot）
- 价差数据（SpreadCurrent / SpreadDirectionCurrent / SpreadBucket / SpreadSnapshot）
- 套利机会（ArbitrageOpportunity）
- 对冲组合及事件（HedgeGroup / HedgeGroupEvent）
- 订单与成交（Order / Fill）
- 盈亏快照（PnlSnapshot）
- 系统日志（SystemLog）
- 风控事件（RiskEvent）
- 告警（Alert）
- 工作线程运行记录（WorkerRun）

所有时间戳字段统一使用 app.core.time_utils.utc_now() 生成默认值。
Schema 迁移由 Alembic 管理，请勿手动修改数据库结构。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.time_utils import utc_now


class Base(DeclarativeBase):
    """所有 ORM 模型的基类，提供 metadata 给 Alembic 使用。"""
    pass


class TimestampMixin:
    """时间戳混入类，为模型自动添加 created_at 和 updated_at 字段。

    - created_at: 记录创建时间
    - updated_at: 记录最后更新时间（每次修改自动刷新）
    """
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)


# ---------------------------------------------------------------------------
# 用户与权限
# ---------------------------------------------------------------------------

class User(Base, TimestampMixin):
    """用户表。

    存储系统登录用户信息，支持基于角色的访问控制（RBAC）。
    关键字段：
    - username: 唯一用户名，用于登录
    - password_hash: 经哈希处理后的密码（不存储明文）
    - role: 用户角色（如 admin / viewer）
    - is_active: 是否启用，可软禁用账户
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

class AuditLog(Base, TimestampMixin):
    """审计日志表。

    记录用户在系统中的关键操作，用于安全审计与问题追溯。
    关键字段：
    - user_id: 操作者用户 ID（可为空，表示系统自动操作）
    - action: 操作类型（如 login / create_order / change_setting）
    - resource: 被操作的资源名称
    - detail: 操作详情（JSON 或文本描述）
    - ip_address: 操作来源 IP 地址
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    action: Mapped[str] = mapped_column(String(128))
    resource: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")


# ---------------------------------------------------------------------------
# 系统配置
# ---------------------------------------------------------------------------

class SystemSetting(Base, TimestampMixin):
    """系统配置表（键值对存储）。

    以 key-value 形式存储全局系统配置，如实盘开关、种子标记等。
    关键字段：
    - key: 配置项名称（主键）
    - value: 配置项值（文本存储，按需转换类型）
    """
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 交易所凭据
# ---------------------------------------------------------------------------

class ExchangeCredential(Base, TimestampMixin):
    """交易所凭据表。

    存储各交易场所（Hyperliquid、MT5 等）的 API 密钥及连接状态。
    凭据经过加密后存储，同时记录指纹用于检测变更。
    关键字段：
    - venue: 交易所标识（如 hyperliquid / mt5），唯一
    - display_name: 前端显示名称
    - environment: 交易所连接环境（live / testnet / demo）
    - enabled: 是否启用
    - read_only: 是否为只读凭据
    - encrypted_credentials: 加密后的凭据内容
    - credentials_fingerprint: 凭据指纹，用于快速判断是否变更
    - last_test_status: 最近一次连通性测试结果状态
    - last_tested_at: 最近一次测试时间
    """
    __tablename__ = "exchange_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    environment: Mapped[str] = mapped_column(String(32), default="live")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    encrypted_credentials: Mapped[str] = mapped_column(Text, default="")
    credentials_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    last_test_status: Mapped[str] = mapped_column(String(32), default="untested")
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# 策略参数
# ---------------------------------------------------------------------------

class StrategySetting(Base, TimestampMixin):
    """策略参数配置表。

    存储套利策略的核心运行参数，包括信号模式、入场/出场阈值、
    自动平仓、自动执行、模拟延迟、执行断路器等。
    系统启动时读取，前端可动态修改。
    关键字段：
    - min_net_profit / min_annualized_return: 最低净利润 / 年化收益率门槛
    - signal_mode: 信号模式（statistical / simple）
    - reachable_entry_*: 可达性入场过滤参数
    - cost_guard_percentile: 成本保护分位数
    - auto_close_*: 自动平仓相关参数
    - auto_execute_*: 自动执行（下单）相关参数
    - paper_*: 模拟交易延迟模拟参数
    - cb_*: 执行断路器（Circuit Breaker）参数
    - execution_mode: 执行模式（paper / live）
    """
    __tablename__ = "strategy_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_net_profit: Mapped[float] = mapped_column(Float, default=5.0)
    min_annualized_return: Mapped[float] = mapped_column(Float, default=0.08)
    signal_mode: Mapped[str] = mapped_column(String(32), default="statistical")
    statistical_lookback_range: Mapped[str] = mapped_column(String(16), default="1h")
    statistical_min_samples: Mapped[int] = mapped_column(Integer, default=200)
    reachable_entry_percentile: Mapped[float] = mapped_column(Float, default=0.75)
    reachable_entry_zscore: Mapped[float] = mapped_column(Float, default=1.0)
    cost_guard_percentile: Mapped[float] = mapped_column(Float, default=0.90)
    min_unit_edge: Mapped[float] = mapped_column(Float, default=0.0)
    min_total_profit: Mapped[float] = mapped_column(Float, default=0.5)
    auto_close_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_close_live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    exit_target_percentile: Mapped[float] = mapped_column(Float, default=0.25)
    auto_close_unit_profit_buffer: Mapped[float] = mapped_column(Float, default=0.0)
    auto_close_min_profit: Mapped[float] = mapped_column(Float, default=0.0)
    default_notional: Mapped[float] = mapped_column(Float, default=1000.0)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, default=240)
    execution_mode: Mapped[str] = mapped_column(String(32), default="paper")
    paper_use_live_account_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_execute_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_execute_paper_only: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_execute_min_hold_ms: Mapped[int] = mapped_column(Integer, default=300)
    auto_execute_confirm_ticks: Mapped[int] = mapped_column(Integer, default=2)
    auto_execute_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30)
    auto_execute_max_per_symbol_open_groups: Mapped[int] = mapped_column(Integer, default=1)
    auto_execute_max_global_open_groups: Mapped[int] = mapped_column(Integer, default=3)
    auto_execute_min_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    paper_decision_delay_ms_min: Mapped[int] = mapped_column(Integer, default=50)
    paper_decision_delay_ms_max: Mapped[int] = mapped_column(Integer, default=200)
    paper_leg_a_latency_ms_min: Mapped[int] = mapped_column(Integer, default=80)
    paper_leg_a_latency_ms_max: Mapped[int] = mapped_column(Integer, default=200)
    paper_leg_b_latency_ms_min: Mapped[int] = mapped_column(Integer, default=120)
    paper_leg_b_latency_ms_max: Mapped[int] = mapped_column(Integer, default=350)
    # 执行断路器（Circuit Breaker）参数
    cb_cooldown_seconds: Mapped[float] = mapped_column(Float, default=3.0)
    cb_initial_threshold: Mapped[float] = mapped_column(Float, default=0.75)
    cb_baseline_multiplier: Mapped[float] = mapped_column(Float, default=2.0)
    cb_min_baseline_samples: Mapped[int] = mapped_column(Integer, default=50)
    cb_detection_seconds: Mapped[float] = mapped_column(Float, default=5.0)


# ---------------------------------------------------------------------------
# 风控参数
# ---------------------------------------------------------------------------

class RiskSetting(Base, TimestampMixin):
    """风控参数配置表。

    存储系统级风控限制，防止超额下单或过度杠杆。
    关键字段：
    - mode: 风控模式（normal / strict / disabled）
    - max_order_notional: 单笔订单最大名义价值
    - max_symbol_exposure: 单品种最大敞口
    - max_total_leverage: 系统最大总杠杆
    - max_new_margin_fraction: 新单最大保证金占比
    - new_order_leverage: 新单默认杠杆
    - min_margin_ratio: 最低保证金比率（低于则触发告警）
    - max_slippage_bps: 最大滑点（基点）
    - max_market_age_seconds: 行情最大有效期（秒）
    - max_api_errors: 最大连续 API 错误次数（超过则暂停）
    """
    __tablename__ = "risk_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(32), default="normal")
    max_order_notional: Mapped[float] = mapped_column(Float, default=2000.0)
    max_symbol_exposure: Mapped[float] = mapped_column(Float, default=5000.0)
    max_total_leverage: Mapped[float] = mapped_column(Float, default=2.0)
    max_new_margin_fraction: Mapped[float] = mapped_column(Float, default=0.30)
    new_order_leverage: Mapped[float] = mapped_column(Float, default=20.0)
    min_margin_ratio: Mapped[float] = mapped_column(Float, default=0.35)
    max_slippage_bps: Mapped[float] = mapped_column(Float, default=8.0)
    max_market_age_seconds: Mapped[int] = mapped_column(Integer, default=10)
    max_api_errors: Mapped[int] = mapped_column(Integer, default=3)
    max_total_open_notional: Mapped[float] = mapped_column(Float, default=10000.0)
    max_global_open_groups: Mapped[int] = mapped_column(Integer, default=3)
    max_pending_open_groups: Mapped[int] = mapped_column(Integer, default=2)
    max_daily_loss: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------
# 品种映射
# ---------------------------------------------------------------------------

class SymbolMapping(Base, TimestampMixin):
    """品种映射表。

    定义套利腿 A（如 Hyperliquid 永续）与腿 B（如 MT5 CFD）之间的品种对应关系，
    以及各交易所侧的交易参数（精度、最小下单量、订单类型、会话时间等）。
    关键字段：
    - symbol: 统一品种代码（唯一，如 BTCUSD）
    - leg_a_venue_symbol / mt5_symbol: 各交易所侧的实际交易代码
    - leg_a_venue / leg_b_venue: 各腿所属交易所
    - base_asset / quote_asset: 基础资产 / 报价资产
    - contract_multiplier: 合约乘数
    - mt5_*: MT5 侧特有参数（最小手数、步长、合约规模、币种、计算模式等）
    - hl_*: Hyperliquid 侧特有参数（订单类型、Post-Only、偏移、TTL 等）
    - mt5_session_*: MT5 交易时段配置（会话模板、时区、常规/仅平仓时段 JSON）
    - execution_style: 泛化执行模式（simultaneous_market / maker_then_market）
    - maker_*: Maker 腿、挂单偏移、TTL 与到期动作
    - single_leg_action: 单腿成交时的处理策略
    - enabled: 是否启用该品种
    """
    __tablename__ = "symbol_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    leg_a_venue_symbol: Mapped[str] = mapped_column(String(64))
    mt5_symbol: Mapped[str] = mapped_column(String(64))
    leg_a_venue: Mapped[str] = mapped_column(String(32), default="hyperliquid")
    leg_a_symbol: Mapped[str] = mapped_column(String(64), default="")
    leg_b_venue: Mapped[str] = mapped_column(String(32), default="mt5")
    leg_b_symbol: Mapped[str] = mapped_column(String(64), default="")
    base_asset: Mapped[str] = mapped_column(String(32), default="")
    quote_asset: Mapped[str] = mapped_column(String(32), default="USD")
    contract_multiplier: Mapped[float] = mapped_column(Float, default=1.0)
    min_order_size: Mapped[float] = mapped_column(Float, default=0.001)
    min_entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    max_close_spread: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_min_lot: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_volume_step: Mapped[float] = mapped_column(Float, default=0.0)
    mt5_contract_size: Mapped[float] = mapped_column(Float, default=1.0)
    mt5_currency_base: Mapped[str] = mapped_column(String(16), default="")
    mt5_currency_profit: Mapped[str] = mapped_column(String(16), default="USD")
    mt5_currency_margin: Mapped[str] = mapped_column(String(16), default="USD")
    mt5_calc_mode: Mapped[int] = mapped_column(Integer, default=0)
    mt5_min_base_size: Mapped[float] = mapped_column(Float, default=0.0)
    leg_a_min_base_size: Mapped[float] = mapped_column(Float, default=0.0)
    leg_a_min_notional: Mapped[float] = mapped_column(Float, default=10.0)
    target_notional: Mapped[float] = mapped_column(Float, default=1000.0)
    max_open_notional: Mapped[float] = mapped_column(Float, default=5000.0)
    max_open_groups: Mapped[int] = mapped_column(Integer, default=1)
    open_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=30)
    max_daily_opens: Mapped[int] = mapped_column(Integer, default=0)
    max_daily_open_notional: Mapped[float] = mapped_column(Float, default=0.0)
    allow_opposite_direction: Mapped[bool] = mapped_column(Boolean, default=False)
    max_holding_minutes: Mapped[int] = mapped_column(Integer, default=240)
    execution_style: Mapped[str] = mapped_column(String(64), default="simultaneous_market")
    maker_leg: Mapped[str] = mapped_column(String(1), default="a")
    maker_offset_bps: Mapped[float] = mapped_column(Float, default=1.0)
    maker_order_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3)
    maker_unfilled_action: Mapped[str] = mapped_column(String(32), default="cancel")
    leg_a_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    leg_b_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    hl_open_order_type: Mapped[str] = mapped_column(String(16), default="market")
    hl_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    hl_post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    hl_maker_offset_bps: Mapped[float] = mapped_column(Float, default=1.0)
    hl_order_ttl_seconds: Mapped[int] = mapped_column(Integer, default=3)
    hl_unfilled_action: Mapped[str] = mapped_column(String(32), default="cancel")
    single_leg_action: Mapped[str] = mapped_column(String(32), default="manual_intervention")
    mt5_open_order_type: Mapped[str] = mapped_column(String(16), default="market")
    mt5_close_order_type: Mapped[str] = mapped_column(String(16), default="market")
    mt5_session_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mt5_session_auto_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    mt5_session_template: Mapped[str] = mapped_column(String(64), default="auto")
    mt5_session_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    mt5_regular_sessions_json: Mapped[str] = mapped_column(Text, default="[]")
    mt5_close_only_sessions_json: Mapped[str] = mapped_column(Text, default="[]")
    mt5_quote_only_sessions_json: Mapped[str] = mapped_column(Text, default="[]")
    mt5_session_source: Mapped[str] = mapped_column(String(64), default="manual")
    mt5_session_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    mt5_pre_close_no_open_minutes: Mapped[int] = mapped_column(Integer, default=15)
    mt5_post_open_cooldown_minutes: Mapped[int] = mapped_column(Integer, default=10)
    allow_hold_through_mt5_close: Mapped[bool] = mapped_column(Boolean, default=False)
    quantity_precision: Mapped[int] = mapped_column(Integer, default=4)
    price_precision: Mapped[int] = mapped_column(Integer, default=2)
    min_tick: Mapped[float] = mapped_column(Float, default=0.01)
    max_slippage_bps: Mapped[float] = mapped_column(Float, default=8.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------------------------------------------------------------------------
# 账户快照
# ---------------------------------------------------------------------------

class AccountSnapshot(Base, TimestampMixin):
    """账户快照表。

    定期记录各平台（Hyperliquid / MT5）的账户权益与余额信息，
    用于前端展示和盈亏分析。
    关键字段：
    - platform: 平台标识（hyperliquid / mt5）
    - equity: 账户总权益
    - available_balance: 可用余额
    - margin_used / margin_ratio: 已用保证金 / 保证金比率
    - portfolio_value: 组合总价值（含现货+永续）
    - perp_equity: 永续合约权益
    - spot_balance / spot_hold: 现货余额 / 冻结
    - withdrawable / free_collateral: 可提资金 / 自由保证金
    - data_source: 数据来源标识
    """
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    equity: Mapped[float] = mapped_column(Float)
    available_balance: Mapped[float] = mapped_column(Float)
    margin_used: Mapped[float] = mapped_column(Float, default=0.0)
    margin_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    currency: Mapped[str] = mapped_column(String(16), default="USD")
    portfolio_value: Mapped[float] = mapped_column(Float, default=0.0)
    perp_equity: Mapped[float] = mapped_column(Float, default=0.0)
    spot_balance: Mapped[float] = mapped_column(Float, default=0.0)
    spot_hold: Mapped[float] = mapped_column(Float, default=0.0)
    withdrawable: Mapped[float] = mapped_column(Float, default=0.0)
    free_collateral: Mapped[float] = mapped_column(Float, default=0.0)
    data_source: Mapped[str] = mapped_column(String(64), default="")


# ---------------------------------------------------------------------------
# 持仓
# ---------------------------------------------------------------------------

class Position(Base, TimestampMixin):
    """持仓表。

    记录各平台上的当前持仓信息，由同步任务定期刷新。
    关键字段：
    - platform: 平台标识
    - symbol: 品种代码
    - side: 持仓方向（long / short）
    - quantity: 持仓数量
    - entry_price: 入场均价
    - mark_price: 当前标记价格
    - unrealized_pnl: 未实现盈亏
    - margin_used: 占用保证金
    - liquidation_price: 强平价格（可为空）
    """
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    margin_used: Mapped[float] = mapped_column(Float, default=0.0)
    liquidation_price: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------------------
# 行情快照
# ---------------------------------------------------------------------------

class MarketSnapshot(Base, TimestampMixin):
    """行情快照表。

    记录各平台各品种的实时买卖盘快照，用于价差计算与分析。
    关键字段：
    - platform: 平台标识
    - symbol: 品种代码
    - bid / ask / mid: 买价 / 卖价 / 中间价
    - depth_notional: 行情深度（名义价值）
    - captured_at: 数据采集时间
    """
    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    bid: Mapped[float] = mapped_column(Float)
    ask: Mapped[float] = mapped_column(Float)
    mid: Mapped[float] = mapped_column(Float)
    depth_notional: Mapped[float] = mapped_column(Float, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


# ---------------------------------------------------------------------------
# 价差数据
# ---------------------------------------------------------------------------

class SpreadCurrent(Base, TimestampMixin):
    """当前价差表（按品种唯一）。

    实时记录每个品种的最新价差状态，每个品种只保留一条最新记录。
    关键字段：
    - symbol: 品种代码（唯一）
    - direction: 套利方向（如 hyperliquid_long_mt5_short）
    - leg_a_bid/ask, leg_b_bid/ask: 两腿的买卖价
    - quantity / leg_a_quantity / leg_b_quantity: 计算用的数量
    - gross_spread: 毛价差
    - entry_spread / close_spread / mid_spread: 入场/出场/中间价差
    - spread_cost / unit_cost: 价差成本 / 单位成本
    - unit_net_profit: 单位净利润
    - total_cost / net_profit: 总成本 / 总净利润
    - annualized_return: 年化收益率
    - status: 状态（candidate / active / rejected 等）
    - reason: 状态原因说明
    - sampled_at: 采样时间
    """
    __tablename__ = "spread_current"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(32))
    leg_a_bid: Mapped[float] = mapped_column(Float)
    leg_a_ask: Mapped[float] = mapped_column(Float)
    leg_b_bid: Mapped[float] = mapped_column(Float)
    leg_b_ask: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_b_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_a_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    close_spread: Mapped[float] = mapped_column(Float, default=0.0)
    mid_spread: Mapped[float] = mapped_column(Float, default=0.0)
    spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SpreadDirectionCurrent(Base, TimestampMixin):
    """分方向当前价差表。

    与 spread_current 类似，但按（品种 + 方向）组合存储，
    支持同一品种同时记录多个方向的价差数据。
    关键字段含义同 SpreadCurrent。
    """
    __tablename__ = "spread_direction_current"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32), index=True)
    leg_a_bid: Mapped[float] = mapped_column(Float)
    leg_a_ask: Mapped[float] = mapped_column(Float)
    leg_b_bid: Mapped[float] = mapped_column(Float)
    leg_b_ask: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_b_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_a_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float, default=0.0)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    close_spread: Mapped[float] = mapped_column(Float, default=0.0)
    mid_spread: Mapped[float] = mapped_column(Float, default=0.0)
    spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    annualized_return: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    reason: Mapped[str] = mapped_column(Text, default="")
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SpreadBucket(Base, TimestampMixin):
    """价差时间桶表（OHLC 聚合）。

    将价差时序数据按固定时间窗口（默认 5 秒）聚合为 OHLC 桶，
    用于前端图表展示和统计信号计算。
    关键字段：
    - symbol / direction: 品种与方向
    - bucket_start: 桶起始时间
    - bucket_seconds: 桶时间跨度（秒）
    - open_spread / high_spread / low_spread / close_spread: 价差 OHLC
    - avg_spread: 桶内平均价差
    - entry_spread / avg_entry_spread / avg_close_basis_spread / avg_mid_spread: 各维度平均价差
    - avg_spread_cost / avg_unit_cost / avg_unit_net_profit: 平均成本与利润
    - sample_count: 桶内采样数量
    """
    __tablename__ = "spread_buckets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32), index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=5)
    open_spread: Mapped[float] = mapped_column(Float)
    high_spread: Mapped[float] = mapped_column(Float)
    low_spread: Mapped[float] = mapped_column(Float)
    close_spread: Mapped[float] = mapped_column(Float)
    avg_spread: Mapped[float] = mapped_column(Float)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    avg_entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    avg_close_basis_spread: Mapped[float] = mapped_column(Float, default=0.0)
    avg_mid_spread: Mapped[float] = mapped_column(Float, default=0.0)
    avg_spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    avg_unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    avg_unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)


class SpreadSnapshot(Base, TimestampMixin):
    """价差历史快照表。

    每次价差采样时追加一条记录，用于历史回溯与统计分析。
    关键字段含义同 SpreadCurrent。
    """
    __tablename__ = "spread_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    leg_a_bid: Mapped[float] = mapped_column(Float)
    leg_a_ask: Mapped[float] = mapped_column(Float)
    leg_b_bid: Mapped[float] = mapped_column(Float)
    leg_b_ask: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_b_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_a_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    close_spread: Mapped[float] = mapped_column(Float, default=0.0)
    mid_spread: Mapped[float] = mapped_column(Float, default=0.0)
    spread_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 套利机会
# ---------------------------------------------------------------------------

class ArbitrageOpportunity(Base, TimestampMixin):
    """套利机会记录表。

    当价差满足入场条件时，系统生成一条套利机会记录，
    记录触发时的市场状态与阈值参数。
    关键字段：
    - symbol / direction: 品种与套利方向
    - notional / quantity: 名义价值与数量
    - trigger_leg_a_bid/ask, trigger_leg_b_bid/ask: 触发时各腿价格
    - gross_spread / total_cost / net_profit / annualized_return: 收益指标
    - entry_threshold / exit_target / overheat_threshold: 入场/出场/过热阈值
    - signal_sample_count: 信号采样次数
    - status: 状态（candidate / executed / expired / rejected）
    - reject_reason: 拒绝原因
    """
    __tablename__ = "arbitrage_opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    notional: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    leg_b_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_a_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    notional_currency: Mapped[str] = mapped_column(String(16), default="USD")
    fx_rate_to_usd: Mapped[float] = mapped_column(Float, default=1.0)
    gross_spread: Mapped[float] = mapped_column(Float)
    trigger_leg_a_bid: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_a_ask: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_b_bid: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_b_ask: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    unit_net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float)
    net_profit: Mapped[float] = mapped_column(Float)
    annualized_return: Mapped[float] = mapped_column(Float)
    entry_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    exit_target: Mapped[float] = mapped_column(Float, default=0.0)
    overheat_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    signal_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    reject_reason: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 对冲组合
# ---------------------------------------------------------------------------

class HedgeGroup(Base, TimestampMixin):
    """对冲组合表。

    记录一次完整的两腿套利对冲操作，从开仓到平仓的全生命周期。
    关键字段：
    - symbol / direction: 品种与套利方向
    - status: 生命周期状态（pending_open / opening / open / closing / closed / error）
    - execution_mode: 执行模式（paper / live）
    - notional / quantity: 名义价值与数量
    - open_cost / realized_pnl / unrealized_pnl: 开仓成本 / 已实现盈亏 / 未实现盈亏
    - fees / funding / swap: 手续费 / 资金费 / 掉期
    - trigger_*: 触发时的市场数据快照
    - entry_spread / entry_threshold / exit_target / overheat_threshold: 价差阈值
    - close_reason: 平仓原因
    - opened_at / closed_at: 开仓/平仓时间
    - source: 来源（system / manual）
    """
    __tablename__ = "hedge_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending_open")
    execution_mode: Mapped[str] = mapped_column(String(32), default="paper")
    execution_profile: Mapped[str] = mapped_column(String(32), default="hybrid_probe_demo")
    notional: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    leg_b_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    leg_a_quantity: Mapped[float] = mapped_column(Float, default=1.0)
    open_cost: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding: Mapped[float] = mapped_column(Float, default=0.0)
    swap: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_spread: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_a_bid: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_a_ask: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_b_bid: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_leg_b_ask: Mapped[float] = mapped_column(Float, default=0.0)
    entry_spread: Mapped[float] = mapped_column(Float, default=0.0)
    entry_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    exit_target: Mapped[float] = mapped_column(Float, default=0.0)
    overheat_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    close_reason: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="system")
    events: Mapped[list["HedgeGroupEvent"]] = relationship(back_populates="hedge_group")


class HedgeGroupEvent(Base, TimestampMixin):
    """对冲组合事件表。

    记录对冲组合生命周期中的每个关键事件（开仓、状态变更、平仓等），
    用于审计追踪与前端时间线展示。
    关键字段：
    - hedge_group_id: 所属对冲组合 ID（外键）
    - event_type: 事件类型（如 opened / closed / error / status_change）
    - detail: 事件详情（JSON 或文本）
    """
    __tablename__ = "hedge_group_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int] = mapped_column(ForeignKey("hedge_groups.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")
    hedge_group: Mapped[HedgeGroup] = relationship(back_populates="events")


# ---------------------------------------------------------------------------
# 订单与成交
# ---------------------------------------------------------------------------

class Order(Base, TimestampMixin):
    """订单表。

    记录所有发往各交易所的订单，包括模拟订单。
    关键字段：
    - hedge_group_id: 关联的对冲组合 ID（可为空）
    - platform: 目标平台
    - symbol: 品种代码
    - side: 订单方向（buy / sell）
    - order_type: 订单类型（market / limit）
    - post_only / reduce_only: 订单标志
    - ttl_seconds: 订单有效期（秒），0 表示不超时
    - quantity / price: 数量 / 价格
    - status: 订单状态（new / submitted / filled / cancelled / error）
    - external_order_id: 交易所返回的订单 ID
    - error_message: 错误信息
    """
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="new")
    external_order_id: Mapped[str] = mapped_column(String(128), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")


class Fill(Base, TimestampMixin):
    """成交记录表。

    记录每笔实际成交明细，与订单关联。
    关键字段：
    - order_id: 关联订单 ID
    - platform: 成交平台
    - symbol: 品种代码
    - side: 成交方向
    - quantity / price: 成交数量 / 成交价格
    - fee: 手续费
    """
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, index=True)
    platform: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------
# 下一代执行编排与事件投影
# ---------------------------------------------------------------------------

class ExecutionIntent(Base, TimestampMixin):
    """一次不可变的业务执行意图，例如开仓、平仓、补偿或 Probe。"""
    __tablename__ = "execution_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int | None] = mapped_column(ForeignKey("hedge_groups.id"), nullable=True, index=True)
    intent_type: Mapped[str] = mapped_column(String(32), index=True)
    execution_mode: Mapped[str] = mapped_column(String(16), default="paper", index=True)
    execution_style: Mapped[str] = mapped_column(String(32), default="simultaneous_market", index=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="system")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    expected_group_version: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionLeg(Base, TimestampMixin):
    """执行意图中的单条 venue 腿，明确动作、仓位侧与不同数量语义。"""
    __tablename__ = "execution_legs"
    __table_args__ = (UniqueConstraint("intent_id", "leg_key", name="uq_execution_leg_intent_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("execution_intents.id"), index=True)
    parent_leg_id: Mapped[int | None] = mapped_column(ForeignKey("execution_legs.id"), nullable=True, index=True)
    leg_key: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(32), default="PRIMARY", index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    venue: Mapped[str] = mapped_column(String(32), index=True)
    instrument_id: Mapped[str] = mapped_column(String(128), default="")
    venue_symbol: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    position_side: Mapped[str] = mapped_column(String(16), default="NET")
    order_side: Mapped[str] = mapped_column(String(16))
    strategy_quantity: Mapped[float] = mapped_column(Float)
    venue_order_quantity: Mapped[float] = mapped_column(Float)
    target_position_quantity_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_position_quantity_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(16), default="GTC")
    post_only: Mapped[bool] = mapped_column(Boolean, default=False)
    venue_reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="PLANNED", index=True)


class VenueOrder(Base, TimestampMixin):
    """交易所订单投影，保留场所标识、PositionId 与累计成交事实。"""
    __tablename__ = "venue_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    execution_leg_id: Mapped[int] = mapped_column(ForeignKey("execution_legs.id"), index=True)
    legacy_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True, index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    venue_order_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    command_id: Mapped[str] = mapped_column(String(64), default="")
    correlation_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    position_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    status: Mapped[str] = mapped_column(String(32), default="INITIALIZED", index=True)
    requested_quantity: Mapped[float] = mapped_column(Float)
    filled_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_quantity: Mapped[float] = mapped_column(Float)
    average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    reconciliation_state: Mapped[str] = mapped_column(String(32), default="LOCAL")
    raw_last_report: Mapped[str] = mapped_column(Text, default="")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionEvent(Base, TimestampMixin):
    """不可变执行事件，用于去重、审计和重建订单投影。"""
    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    intent_id: Mapped[int | None] = mapped_column(ForeignKey("execution_intents.id"), nullable=True, index=True)
    execution_leg_id: Mapped[int | None] = mapped_column(ForeignKey("execution_legs.id"), nullable=True, index=True)
    venue_order_id_ref: Mapped[int | None] = mapped_column(ForeignKey("venue_orders.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    venue_order_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    ts_event: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ts_init: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reconciliation: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutionPositionSnapshot(Base, TimestampMixin):
    """执行侧仓位快照，按账户、instrument 和 PositionId 唯一标识。"""
    __tablename__ = "execution_position_snapshots"
    __table_args__ = (
        UniqueConstraint("venue", "account_id", "instrument_id", "position_id", name="uq_exec_position_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32), index=True)
    account_id: Mapped[str] = mapped_column(String(128), default="")
    instrument_id: Mapped[str] = mapped_column(String(128), index=True)
    position_id: Mapped[str] = mapped_column(String(160), default="")
    position_side: Mapped[str] = mapped_column(String(16), default="NET")
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    mark_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_event_id: Mapped[str] = mapped_column(String(64), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    is_fresh: Mapped[bool] = mapped_column(Boolean, default=True)


class ProbeRun(Base, TimestampMixin):
    """独立 Paper Probe 生命周期；真实最小敞口必须在同一 Run 内回平。"""
    __tablename__ = "probe_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    hedge_group_id: Mapped[int | None] = mapped_column(ForeignKey("hedge_groups.id"), nullable=True, index=True)
    entry_intent_id: Mapped[int | None] = mapped_column(ForeignKey("execution_intents.id"), nullable=True, index=True)
    exit_intent_id: Mapped[int | None] = mapped_column(ForeignKey("execution_intents.id"), nullable=True, index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    venue: Mapped[str] = mapped_column(String(32), index=True)
    instrument_id: Mapped[str] = mapped_column(String(128), index=True)
    position_side: Mapped[str] = mapped_column(String(16), default="NET")
    entry_side: Mapped[str] = mapped_column(String(16), default="BUY")
    probe_quantity: Mapped[float] = mapped_column(Float)
    open_venue_order_id: Mapped[int | None] = mapped_column(ForeignKey("venue_orders.id"), nullable=True)
    close_venue_order_id: Mapped[int | None] = mapped_column(ForeignKey("venue_orders.id"), nullable=True)
    open_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    residual_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    baseline_position_quantity: Mapped[float] = mapped_column(Float, default=0.0)
    final_position_quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    flat_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="CREATED", index=True)
    error_message: Mapped[str] = mapped_column(Text, default="")


class ExecutionOutbox(Base, TimestampMixin):
    """与 Intent 同事务写入的待发送命令，提供可靠投递和幂等重试。"""
    __tablename__ = "execution_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("execution_intents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 盈亏快照
# ---------------------------------------------------------------------------

class PnlSnapshot(Base, TimestampMixin):
    """盈亏快照表。

    定期记录对冲组合维度的权益与盈亏数据，用于绘制 PnL 曲线。
    关键字段：
    - hedge_group_id: 关联对冲组合 ID（可为空，表示全局快照）
    - equity: 总权益
    - realized_pnl / unrealized_pnl: 已实现 / 未实现盈亏
    """
    __tablename__ = "pnl_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hedge_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    equity: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------------------
# 系统日志
# ---------------------------------------------------------------------------

class SystemLog(Base, TimestampMixin):
    """系统日志表。

    记录系统运行过程中的各类日志，按级别和类别分类。
    关键字段：
    - level: 日志级别（info / warning / error / critical）
    - category: 日志类别（如 engine / risk / execution / market）
    - message: 日志消息
    - context: 附加上下文（JSON 格式）
    """
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 风控事件
# ---------------------------------------------------------------------------

class RiskEvent(Base, TimestampMixin):
    """风控事件表。

    记录风控规则触发的事件，用于审计和前端告警展示。
    关键字段：
    - level: 事件级别（warning / critical）
    - rule: 触发的风控规则名称
    - message: 事件描述
    - symbol: 关联品种（可为空）
    """
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="warning")
    rule: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(String(32), default="")


# ---------------------------------------------------------------------------
# 告警
# ---------------------------------------------------------------------------

class Alert(Base, TimestampMixin):
    """告警表。

    存储系统产生的各类告警信息，支持确认（acknowledged）操作。
    关键字段：
    - level: 告警级别（info / warning / critical）
    - title: 告警标题
    - message: 告警详情
    - acknowledged: 是否已被确认
    """
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default="info")
    title: Mapped[str] = mapped_column(String(128))
    message: Mapped[str] = mapped_column(Text)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# 工作线程运行记录
# ---------------------------------------------------------------------------

class WorkerRun(Base, TimestampMixin):
    """工作线程运行记录表。

    记录各后台工作线程每次运行的状态与耗时，用于监控与排查。
    关键字段：
    - worker_name: 工作线程名称（如 spread_collector / position_sync）
    - status: 运行状态（success / error / timeout）
    - duration_ms: 运行耗时（毫秒）
    - error_message: 错误信息（成功时为空）
    """
    __tablename__ = "worker_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_name: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------------------
# 复合索引（PostgreSQL 友好）
# ---------------------------------------------------------------------------

Index("ix_hedge_groups_symbol_status", HedgeGroup.symbol, HedgeGroup.status)
Index("ix_hedge_groups_status_opened", HedgeGroup.status, HedgeGroup.opened_at)
Index("ix_hedge_group_events_group_id", HedgeGroupEvent.hedge_group_id)
Index("ix_orders_group_platform", Order.hedge_group_id, Order.platform)
Index("ix_fills_order_platform", Fill.order_id, Fill.platform)
Index("ix_spread_snapshots_symbol_created", SpreadSnapshot.symbol, SpreadSnapshot.created_at)
Index("ix_spread_buckets_symbol_dir_start", SpreadBucket.symbol, SpreadBucket.direction, SpreadBucket.bucket_start)
Index("ix_system_logs_created_at", SystemLog.created_at)
Index("ix_arbitrage_opps_symbol_status", ArbitrageOpportunity.symbol, ArbitrageOpportunity.status)
Index("ix_market_snapshots_symbol_platform", MarketSnapshot.symbol, MarketSnapshot.platform)
Index("ix_positions_platform_symbol", Position.platform, Position.symbol)
Index("ix_risk_events_created_at", RiskEvent.created_at)
Index("ix_orders_created_id", Order.created_at, Order.id)
Index("ix_fills_created_id", Fill.created_at, Fill.id)
Index("ix_alerts_created_id", Alert.created_at, Alert.id)
Index("ix_account_snapshots_platform_created", AccountSnapshot.platform, AccountSnapshot.created_at, AccountSnapshot.id)
Index("ix_arbitrage_opps_status_profit", ArbitrageOpportunity.status, ArbitrageOpportunity.net_profit)
Index("ix_arbitrage_opps_status_updated", ArbitrageOpportunity.status, ArbitrageOpportunity.updated_at)
Index("ix_worker_runs_name_created", WorkerRun.worker_name, WorkerRun.created_at)
