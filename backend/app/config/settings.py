"""
CrossHedge 配置系统
===================

本模块集中管理应用的全部配置项，按业务领域拆分为多个嵌套 dataclass：

- DatabaseSettings   —— 数据库连接与连接池参数
- SecuritySettings   —— JWT / 管理员 / 加密等安全相关参数
- ExecutionSettings  —— 通用执行参数（滑点、汇率回退、对账等）
- QuoteSettings      —— 行情源与报价同步参数
- ScannerSettings    —— 扫描器调度间隔
- HyperliquidSettings —— Hyperliquid 交易所专属配置
- MT5Settings        —— MetaTrader 5 终端连接与交易参数
- VenueSettings      —— 原生交易所运行时配置
- Settings           —— 根配置，聚合上述所有子配置

环境变量映射
------------
保持与 ``.env`` 文件向后兼容。``_ENV_MAPPING`` 字典将每个环境变量名
显式映射到 ``(父属性名, 子属性名)`` 元组，``get_settings()`` 在加载时
根据该映射表将扁平的环境变量写入对应的嵌套 dataclass 字段。

使用方式::

    from app.config.settings import get_settings
    settings = get_settings()
    print(settings.hyperliquid.info_url)
"""

import os
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit


# ---------------------------------------------------------------------------
# 模块级常量
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parents[3]

HYPERLIQUID_MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERLIQUID_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
HYPERLIQUID_MAINNET_API_URL = "https://api.hyperliquid.xyz"

INSECURE_DEFAULT_JWT_SECRET = "change-me-before-live"
INSECURE_DEFAULT_ADMIN_PASSWORD = "admin123"

LOCAL_ENVIRONMENTS = {"local", "dev", "development", "test", "testing"}


# ---------------------------------------------------------------------------
# 子配置 dataclass
# ---------------------------------------------------------------------------

@dataclass
class DatabaseSettings:
    """数据库配置 —— 连接字符串与 SQLAlchemy 连接池参数"""

    # 数据库连接 URL（DATABASE_URL）
    url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/mt5_hedge"
    # 连接池大小（DATABASE_POOL_SIZE）
    pool_size: int = 10
    # 连接池最大溢出数（DATABASE_MAX_OVERFLOW）
    max_overflow: int = 20
    # 连接回收周期 / 秒（DATABASE_POOL_RECYCLE）
    pool_recycle: int = 3600


@dataclass
class RedisSettings:
    """Redis 缓存、Stream 与 MT5 Gateway 通信配置。"""

    url: str = "redis://localhost:6379/0"
    password: str = ""
    password_file: str = ""
    key_prefix: str = "crosshedge"
    socket_timeout_seconds: float = 5.0
    mt5_rpc_timeout_seconds: float = 15.0
    mt5_snapshot_ttl_seconds: int = 10
    mt5_heartbeat_ttl_seconds: int = 15


@dataclass
class SecuritySettings:
    """安全与认证配置 —— JWT、管理员账户、交易所密钥加密"""

    # JWT 签名密钥（JWT_SECRET）
    jwt_secret: str = INSECURE_DEFAULT_JWT_SECRET
    jwt_secret_file: str = ""
    # JWT 签名算法（JWT_ALGORITHM）
    jwt_algorithm: str = "HS256"
    # 访问令牌有效期 / 分钟（ACCESS_TOKEN_EXPIRE_MINUTES）
    access_token_minutes: int = 720
    # 默认管理员用户名（ADMIN_USERNAME）
    admin_username: str = "admin"
    # 默认管理员密码（ADMIN_PASSWORD）
    admin_password: str = INSECURE_DEFAULT_ADMIN_PASSWORD
    # 交易所配置加密主密钥；留空时使用 jwt_secret 派生（EXCHANGE_CONFIG_SECRET）
    exchange_config_secret: str = ""
    exchange_config_secret_file: str = ""
    # 实盘交易总开关（LIVE_TRADING_ENABLED）
    live_trading_enabled: bool = False
    # 实盘交易二次确认口令，留空则不启用（LIVE_TRADING_CONFIRMATION）
    live_trading_confirmation: str = ""
    # 默认执行模式：paper / live（DEFAULT_EXECUTION_MODE）
    default_execution_mode: str = "paper"
    # 交易品种映射文件路径（SYMBOL_MAPPING_PATH）
    symbol_mapping_path: str = ""

    def __post_init__(self) -> None:
        # symbol_mapping_path 依赖 ROOT_DIR，在默认值中无法直接引用
        if not self.symbol_mapping_path:
            self.symbol_mapping_path = str(ROOT_DIR / "config" / "symbol_mappings.yaml")


@dataclass
class QuoteSettings:
    """行情源与报价同步配置 —— 控制报价来源模式及各同步时钟参数"""

    # 行情来源模式：paper / live（QUOTE_SOURCE_MODE）
    source_mode: str = "paper"
    # Paper 模式下定价轮询间隔 / 毫秒（PAPER_QUOTE_INTERVAL_MS）
    paper_quote_interval_ms: int = 200
    # MT5 报价轮询间隔 / 毫秒（MT5_QUOTE_POLL_INTERVAL_MS）
    mt5_quote_poll_interval_ms: int = 200
    # 宽松报价同步窗口 / 毫秒（LOOSE_QUOTE_SYNC_MS）
    loose_sync_ms: int = 3000
    # 严格报价同步窗口 / 毫秒（STRICT_QUOTE_SYNC_MS）
    strict_sync_ms: int = 500
    # 报价过期阈值 / 毫秒（QUOTE_STALE_MS）
    stale_ms: int = 1500
    # 推送流间隔 / 毫秒（STREAM_INTERVAL_MS）
    stream_interval_ms: int = 1000
    # 信号统计缓存 TTL / 毫秒（SIGNAL_STATS_CACHE_TTL_MS）
    signal_stats_cache_ttl_ms: int = 10000


@dataclass
class ScannerSettings:
    """扫描器调度配置 —— 各定时任务的触发间隔"""

    # 主扫描循环间隔 / 秒（SCANNER_INTERVAL_SECONDS）
    interval_seconds: int = 15
    # 主扫描循环间隔 / 毫秒，>0 时覆盖 interval_seconds（SCANNER_INTERVAL_MS）
    interval_ms: int = 0
    # 状态持久化间隔 / 毫秒（SCAN_PERSIST_INTERVAL_MS）
    persist_interval_ms: int = 1000
    # 执行维护间隔 / 毫秒（EXECUTION_MAINTENANCE_INTERVAL_MS）
    execution_maintenance_interval_ms: int = 1000
    # 价差历史刷新间隔 / 秒（SPREAD_HISTORY_INTERVAL_SECONDS）
    spread_history_interval_seconds: int = 5
    # 价差分桶窗口 / 秒（SPREAD_BUCKET_SECONDS）
    spread_bucket_seconds: int = 5


@dataclass
class HyperliquidSettings:
    """Hyperliquid 交易所配置 —— API 端点、费率、下单开关等"""

    # Info API 端点（HYPERLIQUID_INFO_URL）
    info_url: str = HYPERLIQUID_MAINNET_INFO_URL
    # WebSocket 端点（HYPERLIQUID_WS_URL）
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    # 默认 Taker 费率（HYPERLIQUID_DEFAULT_TAKER_FEE_RATE）
    default_taker_fee_rate: float = 0.00045
    # 默认 Maker 费率（HYPERLIQUID_DEFAULT_MAKER_FEE_RATE）
    default_maker_fee_rate: float = 0.00015
    # 默认最小名义价值（HYPERLIQUID_DEFAULT_MIN_NOTIONAL）
    default_min_notional: float = 10.0
    # 钱包地址（HYPERLIQUID_ACCOUNT_ADDRESS）
    account_address: str = ""
    # API 签名密钥（HYPERLIQUID_SECRET_KEY）
    secret_key: str = ""
    # Paper-live 滑点估计（HYPERLIQUID_PAPER_LIVE_SLIPPAGE）
    paper_live_slippage: float = 0.01


@dataclass
class MT5Settings:
    """MetaTrader 5 配置 —— 终端连接、下单参数、费率与缓存 TTL"""

    # MT5 账户登录号（MT5_LOGIN）
    login: str = ""
    # MT5 账户密码（MT5_PASSWORD）
    password: str = ""
    # MT5 服务器名称（MT5_SERVER）
    server: str = ""
    # 是否启用 MT5 实盘下单（MT5_LIVE_ORDER_ENABLED）
    live_order_enabled: bool = False
    # 是否启用 MT5 模拟下单（MT5_DEMO_ORDER_ENABLED）
    demo_order_enabled: bool = False
    # 下单滑点偏差 / 点（MT5_ORDER_DEVIATION_POINTS）
    order_deviation_points: int = 20
    # EA Magic Number（MT5_ORDER_MAGIC）
    order_magic: int = 260620
    # 活跃订单轮询间隔 / 毫秒（MT5_ORDER_POLL_INTERVAL_MS）
    order_poll_interval_ms: int = 75
    # 会话 Tick 过期阈值 / 秒（MT5_SESSION_TICK_STALE_SECONDS）
    session_tick_stale_seconds: int = 120
    # 交易能力缓存 TTL / 毫秒（MT5_TRADABILITY_CACHE_TTL_MS）
    tradability_cache_ttl_ms: int = 15000
    # 交易能力刷新间隔 / 秒（MT5_TRADABILITY_REFRESH_SECONDS）
    tradability_refresh_seconds: int = 5
    # 交易拒绝隔离期 / 秒（MT5_TRADE_REJECT_QUARANTINE_SECONDS）
    trade_reject_quarantine_seconds: int = 21600
    # 会话模板刷新间隔 / 小时（MT5_SESSION_TEMPLATE_REFRESH_HOURS）
    session_template_refresh_hours: int = 24


@dataclass
class CostSettings:
    """成本与汇率配置 —— 滑点、外汇回退费率、成本缓存"""

    # 默认滑点 / 基点（DEFAULT_SLIPPAGE_BPS）
    default_slippage_bps: float = 0.0
    # 默认外汇成本费率（DEFAULT_FX_COST_RATE）
    default_fx_cost_rate: float = 0.0
    # 外汇回退汇率 JSON（FX_FALLBACK_RATES）
    fx_fallback_rates: str = '{"JPY":0.00625}'
    # 成本缓存 TTL / 秒（COST_CACHE_TTL_SECONDS）
    cost_cache_ttl_seconds: int = 60
    # Carry-cost 同步间隔 / 秒（CARRY_COST_SYNC_INTERVAL_SECONDS）
    carry_cost_sync_interval_seconds: int = 300
    # Binance 无账户凭据时使用的默认 Maker 费率（BINANCE_DEFAULT_MAKER_FEE_RATE）
    binance_default_maker_fee_rate: float = 0.0002
    # Binance 无账户凭据时使用的默认 Taker 费率（BINANCE_DEFAULT_TAKER_FEE_RATE）
    binance_default_taker_fee_rate: float = 0.0005


@dataclass
class PaperLiveSettings:
    """Paper-live 探针框架配置 —— 多 venue 模拟下单参数"""

    # 是否启用 paper-live 探针（PAPER_LIVE_PROBE_ENABLED）
    probe_enabled: bool = False
    # 探针覆盖的 venue 列表，逗号分隔，* 表示全部（PAPER_LIVE_PROBE_VENUES）
    probe_venues: str = "*"


@dataclass
class VenueSettings:
    """原生交易所运行时配置。"""

    # 连接器启动超时 / 秒（VENUE_STARTUP_TIMEOUT_SECONDS）
    startup_timeout_seconds: float = 30.0
    # 品种、费率与资金费定时刷新间隔 / 秒（VENUE_INSTRUMENT_REFRESH_SECONDS）
    instrument_refresh_seconds: int = 21600
    # 账户快照兜底对账间隔 / 秒（VENUE_ACCOUNT_RECONCILE_SECONDS）
    account_reconcile_seconds: int = 60


# ---------------------------------------------------------------------------
# 环境变量 → 嵌套属性映射表
# 格式：环境变量名 → (父 Settings 属性名, 子 dataclass 属性名)
# ---------------------------------------------------------------------------

_ENV_MAPPING: dict[str, tuple[str, str]] = {
    # --- 根级 ---
    "APP_NAME":                          ("", "app_name"),
    "ENVIRONMENT":                       ("", "environment"),
    # --- DatabaseSettings ---
    "DATABASE_URL":                      ("database", "url"),
    "DATABASE_POOL_SIZE":                ("database", "pool_size"),
    "DATABASE_MAX_OVERFLOW":             ("database", "max_overflow"),
    "DATABASE_POOL_RECYCLE":             ("database", "pool_recycle"),
    # --- RedisSettings ---
    "REDIS_URL":                         ("redis", "url"),
    "REDIS_PASSWORD":                    ("redis", "password"),
    "REDIS_PASSWORD_FILE":               ("redis", "password_file"),
    "REDIS_KEY_PREFIX":                  ("redis", "key_prefix"),
    "REDIS_SOCKET_TIMEOUT_SECONDS":      ("redis", "socket_timeout_seconds"),
    "MT5_RPC_TIMEOUT_SECONDS":           ("redis", "mt5_rpc_timeout_seconds"),
    "MT5_SNAPSHOT_TTL_SECONDS":          ("redis", "mt5_snapshot_ttl_seconds"),
    "MT5_HEARTBEAT_TTL_SECONDS":         ("redis", "mt5_heartbeat_ttl_seconds"),
    # --- SecuritySettings ---
    "JWT_SECRET":                        ("security", "jwt_secret"),
    "JWT_SECRET_FILE":                   ("security", "jwt_secret_file"),
    "JWT_ALGORITHM":                     ("security", "jwt_algorithm"),
    "ACCESS_TOKEN_EXPIRE_MINUTES":       ("security", "access_token_minutes"),
    "ADMIN_USERNAME":                    ("security", "admin_username"),
    "ADMIN_PASSWORD":                    ("security", "admin_password"),
    "EXCHANGE_CONFIG_SECRET":            ("security", "exchange_config_secret"),
    "EXCHANGE_CONFIG_SECRET_FILE":       ("security", "exchange_config_secret_file"),
    "LIVE_TRADING_ENABLED":              ("security", "live_trading_enabled"),
    "LIVE_TRADING_CONFIRMATION":         ("security", "live_trading_confirmation"),
    "DEFAULT_EXECUTION_MODE":            ("security", "default_execution_mode"),
    "SYMBOL_MAPPING_PATH":               ("security", "symbol_mapping_path"),
    # --- QuoteSettings ---
    "QUOTE_SOURCE_MODE":                 ("quote", "source_mode"),
    "PAPER_QUOTE_INTERVAL_MS":           ("quote", "paper_quote_interval_ms"),
    "MT5_QUOTE_POLL_INTERVAL_MS":        ("quote", "mt5_quote_poll_interval_ms"),
    "LOOSE_QUOTE_SYNC_MS":               ("quote", "loose_sync_ms"),
    "STRICT_QUOTE_SYNC_MS":              ("quote", "strict_sync_ms"),
    "QUOTE_STALE_MS":                    ("quote", "stale_ms"),
    "STREAM_INTERVAL_MS":                ("quote", "stream_interval_ms"),
    "SIGNAL_STATS_CACHE_TTL_MS":         ("quote", "signal_stats_cache_ttl_ms"),
    # --- ScannerSettings ---
    "SCANNER_INTERVAL_MS":               ("scanner", "interval_ms"),
    "SCAN_PERSIST_INTERVAL_MS":          ("scanner", "persist_interval_ms"),
    "EXECUTION_MAINTENANCE_INTERVAL_MS": ("scanner", "execution_maintenance_interval_ms"),
    "SPREAD_HISTORY_INTERVAL_SECONDS":   ("scanner", "spread_history_interval_seconds"),
    "SPREAD_BUCKET_SECONDS":             ("scanner", "spread_bucket_seconds"),
    # --- HyperliquidSettings ---
    "HYPERLIQUID_DEFAULT_TAKER_FEE_RATE": ("hyperliquid", "default_taker_fee_rate"),
    "HYPERLIQUID_DEFAULT_MAKER_FEE_RATE": ("hyperliquid", "default_maker_fee_rate"),
    "HYPERLIQUID_DEFAULT_MIN_NOTIONAL":  ("hyperliquid", "default_min_notional"),
    "HYPERLIQUID_PAPER_LIVE_SLIPPAGE":   ("hyperliquid", "paper_live_slippage"),
    # --- MT5Settings ---
    "MT5_LOGIN":                         ("mt5", "login"),
    "MT5_PASSWORD":                      ("mt5", "password"),
    "MT5_SERVER":                        ("mt5", "server"),
    "MT5_LIVE_ORDER_ENABLED":            ("mt5", "live_order_enabled"),
    "MT5_DEMO_ORDER_ENABLED":            ("mt5", "demo_order_enabled"),
    "MT5_ORDER_DEVIATION_POINTS":        ("mt5", "order_deviation_points"),
    "MT5_ORDER_MAGIC":                   ("mt5", "order_magic"),
    "MT5_ORDER_POLL_INTERVAL_MS":        ("mt5", "order_poll_interval_ms"),
    "MT5_SESSION_TICK_STALE_SECONDS":    ("mt5", "session_tick_stale_seconds"),
    "MT5_TRADABILITY_CACHE_TTL_MS":      ("mt5", "tradability_cache_ttl_ms"),
    "MT5_TRADABILITY_REFRESH_SECONDS":   ("mt5", "tradability_refresh_seconds"),
    "MT5_TRADE_REJECT_QUARANTINE_SECONDS": ("mt5", "trade_reject_quarantine_seconds"),
    "MT5_SESSION_TEMPLATE_REFRESH_HOURS": ("mt5", "session_template_refresh_hours"),
    # --- CostSettings ---
    "DEFAULT_SLIPPAGE_BPS":              ("cost", "default_slippage_bps"),
    "DEFAULT_FX_COST_RATE":              ("cost", "default_fx_cost_rate"),
    "FX_FALLBACK_RATES":                 ("cost", "fx_fallback_rates"),
    "COST_CACHE_TTL_SECONDS":            ("cost", "cost_cache_ttl_seconds"),
    "CARRY_COST_SYNC_INTERVAL_SECONDS":  ("cost", "carry_cost_sync_interval_seconds"),
    "BINANCE_DEFAULT_MAKER_FEE_RATE":    ("cost", "binance_default_maker_fee_rate"),
    "BINANCE_DEFAULT_TAKER_FEE_RATE":    ("cost", "binance_default_taker_fee_rate"),
    # --- PaperLiveSettings ---
    "PAPER_LIVE_PROBE_ENABLED":          ("paper_live", "probe_enabled"),
    "PAPER_LIVE_PROBE_VENUES":           ("paper_live", "probe_venues"),
    # --- VenueSettings ---
    "VENUE_STARTUP_TIMEOUT_SECONDS":     ("venues", "startup_timeout_seconds"),
    "VENUE_INSTRUMENT_REFRESH_SECONDS":  ("venues", "instrument_refresh_seconds"),
    "VENUE_ACCOUNT_RECONCILE_SECONDS":   ("venues", "account_reconcile_seconds"),
    # --- 执行对账 ---
    "EXECUTION_RECONCILE_PENDING_STALE_SECONDS": ("execution", "reconcile_pending_stale_seconds"),
}


# ---------------------------------------------------------------------------
# 执行对账配置（小分组，仅一个字段）
# ---------------------------------------------------------------------------

@dataclass
class ExecutionSettings:
    """通用执行参数 —— 对账、 reconcile 等"""

    # 待执行对账过期阈值 / 秒（EXECUTION_RECONCILE_PENDING_STALE_SECONDS）
    reconcile_pending_stale_seconds: int = 300


# ---------------------------------------------------------------------------
# 根配置
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """应用根配置 —— 聚合所有领域子配置"""

    # 应用名称（APP_NAME）
    app_name: str = "CrossHedge"
    # 运行环境：local / dev / production 等（ENVIRONMENT）
    environment: str = "local"

    # 数据库配置
    database: DatabaseSettings = field(default_factory=DatabaseSettings)
    # Redis 缓存与跨进程通信
    redis: RedisSettings = field(default_factory=RedisSettings)
    # 安全与认证配置
    security: SecuritySettings = field(default_factory=SecuritySettings)
    # 行情源与报价同步配置
    quote: QuoteSettings = field(default_factory=QuoteSettings)
    # 扫描器调度配置
    scanner: ScannerSettings = field(default_factory=ScannerSettings)
    # Hyperliquid 交易所配置
    hyperliquid: HyperliquidSettings = field(default_factory=HyperliquidSettings)
    # MetaTrader 5 配置
    mt5: MT5Settings = field(default_factory=MT5Settings)
    # 成本与汇率配置
    cost: CostSettings = field(default_factory=CostSettings)
    # Paper-live 探针配置
    paper_live: PaperLiveSettings = field(default_factory=PaperLiveSettings)
    # 原生交易所运行时配置
    venues: VenueSettings = field(default_factory=VenueSettings)
    # 通用执行参数
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _load_env_file() -> dict[str, str]:
    """从项目根目录读取 .env 文件，返回 {变量名: 值} 字典。"""
    path = ROOT_DIR / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _coerce(value: str, default: Any) -> Any:
    """根据默认值的类型将环境变量字符串转换为目标类型。"""
    if isinstance(default, bool):
        return value.lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


def _get_sub_settings(settings: Settings) -> dict[str, Any]:
    """返回 {父属性名: 子 dataclass 实例} 的字典，仅包含 dataclass 字段。"""
    result: dict[str, Any] = {}
    for f in fields(settings):
        sub = getattr(settings, f.name)
        if hasattr(sub, "__dataclass_fields__"):
            result[f.name] = sub
    return result


def _read_secret_file(path: str, variable_name: str) -> str:
    """读取容器密钥文件，并拒绝空文件或缺失文件。"""
    secret_path = Path(path)
    try:
        value = secret_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{variable_name} 指向的密钥文件不可读: {secret_path}") from exc
    if not value:
        raise ValueError(f"{variable_name} 指向的密钥文件为空: {secret_path}")
    return value


def _apply_secret_files(settings: Settings) -> None:
    """让持久化密钥文件覆盖普通环境变量，并为 Redis URL 注入认证信息。"""
    security = settings.security
    if security.jwt_secret_file:
        security.jwt_secret = _read_secret_file(security.jwt_secret_file, "JWT_SECRET_FILE")
    if security.exchange_config_secret_file:
        security.exchange_config_secret = _read_secret_file(
            security.exchange_config_secret_file,
            "EXCHANGE_CONFIG_SECRET_FILE",
        )

    redis = settings.redis
    if redis.password_file:
        redis.password = _read_secret_file(redis.password_file, "REDIS_PASSWORD_FILE")
    if redis.password:
        parts = urlsplit(redis.url)
        if not parts.hostname:
            raise ValueError("REDIS_URL 缺少主机名")
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        port = f":{parts.port}" if parts.port else ""
        redis.url = urlunsplit((
            parts.scheme,
            f":{quote(redis.password, safe='')}@{host}{port}",
            parts.path,
            parts.query,
            parts.fragment,
        ))


def _validate_settings(settings: Settings) -> None:
    """在启动阶段校验关键枚举和时间参数，避免无效配置进入运行期。"""
    if settings.security.default_execution_mode not in {"dry_run", "paper", "live"}:
        raise ValueError("DEFAULT_EXECUTION_MODE 必须是 dry_run、paper 或 live")
    if settings.quote.source_mode not in {"paper", "live"}:
        raise ValueError("QUOTE_SOURCE_MODE 必须是 paper 或 live")
    positive_values = {
        "PAPER_QUOTE_INTERVAL_MS": settings.quote.paper_quote_interval_ms,
        "MT5_QUOTE_POLL_INTERVAL_MS": settings.quote.mt5_quote_poll_interval_ms,
        "LOOSE_QUOTE_SYNC_MS": settings.quote.loose_sync_ms,
        "STRICT_QUOTE_SYNC_MS": settings.quote.strict_sync_ms,
        "QUOTE_STALE_MS": settings.quote.stale_ms,
        "STREAM_INTERVAL_MS": settings.quote.stream_interval_ms,
        "SCAN_PERSIST_INTERVAL_MS": settings.scanner.persist_interval_ms,
        "EXECUTION_MAINTENANCE_INTERVAL_MS": settings.scanner.execution_maintenance_interval_ms,
        "SPREAD_HISTORY_INTERVAL_SECONDS": settings.scanner.spread_history_interval_seconds,
        "SPREAD_BUCKET_SECONDS": settings.scanner.spread_bucket_seconds,
        "COST_CACHE_TTL_SECONDS": settings.cost.cost_cache_ttl_seconds,
        "MT5_RPC_TIMEOUT_SECONDS": settings.redis.mt5_rpc_timeout_seconds,
        "MT5_SNAPSHOT_TTL_SECONDS": settings.redis.mt5_snapshot_ttl_seconds,
        "MT5_HEARTBEAT_TTL_SECONDS": settings.redis.mt5_heartbeat_ttl_seconds,
    }
    invalid = [name for name, value in positive_values.items() if value <= 0]
    if invalid:
        raise ValueError(f"配置必须大于 0: {', '.join(invalid)}")
    if settings.quote.strict_sync_ms > settings.quote.loose_sync_ms:
        raise ValueError("STRICT_QUOTE_SYNC_MS 不能大于 LOOSE_QUOTE_SYNC_MS")


# ---------------------------------------------------------------------------
# 运行时安全检查
# ---------------------------------------------------------------------------

def hyperliquid_execution_info_url(settings: Settings | None = None) -> str:
    """返回 Hyperliquid Info API URL，根据环境自动选择主网 / 测试网。"""
    settings = settings or get_settings()
    return settings.hyperliquid.info_url or HYPERLIQUID_MAINNET_INFO_URL


def insecure_runtime_reasons(settings: Settings) -> list[str]:
    """检查安全配置是否仍为不安全的默认值，返回原因列表。"""
    reasons: list[str] = []
    sec = settings.security
    if not sec.jwt_secret.strip() or sec.jwt_secret == INSECURE_DEFAULT_JWT_SECRET:
        reasons.append("JWT_SECRET 仍为默认值")
    if not sec.admin_password.strip():
        reasons.append("ADMIN_PASSWORD 不能为空")
    elif sec.admin_password == INSECURE_DEFAULT_ADMIN_PASSWORD:
        reasons.append("ADMIN_PASSWORD 仍为默认值")
    return reasons


def runtime_requires_strong_secrets(settings: Settings) -> bool:
    """判断当前运行环境是否需要强随机密钥（非本地环境 或 任何实盘开关已开启）。"""
    environment = settings.environment.strip().lower()
    live_switch_enabled = (
        settings.security.live_trading_enabled
        or settings.mt5.live_order_enabled
        or settings.paper_live.probe_enabled
        or settings.security.default_execution_mode == "live"
    )
    return environment not in LOCAL_ENVIRONMENTS or live_switch_enabled


def enforce_runtime_security(settings: Settings) -> None:
    """生产 / 实盘模式下强制校验密钥安全性，不通过则抛出 RuntimeError。"""
    if not runtime_requires_strong_secrets(settings):
        return
    reasons = insecure_runtime_reasons(settings)
    if reasons:
        detail = "；".join(reasons)
        raise RuntimeError(
            f"不安全启动配置：{detail}。"
            "生产或实盘相关模式必须在 .env 中设置强随机 JWT_SECRET 和非默认管理员密码。"
        )


# ---------------------------------------------------------------------------
# 单例获取
# ---------------------------------------------------------------------------

@lru_cache
def get_settings() -> Settings:
    """
    加载配置单例。

    1. 解析 .env 文件与 os.environ 中的环境变量。
    2. 根据 _ENV_MAPPING 将扁平环境变量写入对应的嵌套 dataclass 字段。
    3. 返回经 lru_cache 缓存的 Settings 实例。
    """
    env_file = _load_env_file()

    # 先创建带默认值的根配置，用于类型推断
    root = Settings()
    sub_map = _get_sub_settings(root)

    # 收集每个子配置的覆盖值: {父属性名: {子属性名: 转换后的值}}
    overrides: dict[str, dict[str, Any]] = {"": {}}
    for env_key, (parent_name, child_name) in _ENV_MAPPING.items():
        raw_value = os.getenv(env_key, env_file.get(env_key))
        if raw_value is None:
            continue

        # 确定默认值用于类型推断
        if parent_name:
            default_val = getattr(sub_map.get(parent_name), child_name, None)
        else:
            default_val = getattr(root, child_name, None)

        coerced = _coerce(raw_value, default_val)

        if parent_name:
            overrides.setdefault(parent_name, {})[child_name] = coerced
        else:
            overrides[""][child_name] = coerced

    # 构建各子配置 dataclass
    init_kwargs: dict[str, Any] = {}
    for parent_name, child_overrides in overrides.items():
        if not parent_name:
            # 根级字段
            init_kwargs.update(child_overrides)
            continue
        sub_cls = type(sub_map[parent_name])
        init_kwargs[parent_name] = sub_cls(**child_overrides)

    settings = Settings(**init_kwargs)
    _apply_secret_files(settings)
    _validate_settings(settings)
    return settings
