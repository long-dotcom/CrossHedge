"""
执行运行时设置模块
==================

管理执行层的运行时可动态调整的配置项（通过数据库 ``SystemSetting`` 表覆盖）：
- ``paper_live_probe_enabled``: Paper-live 探针总开关

配置优先级：数据库 > .env 环境变量 > 默认值

前端通过 API 读取 / 修改这些设置时，会经过本模块的函数进行转换。

使用方式::

    from app.config.settings import get_settings
    from app.execution.runtime_settings import runtime_paper_live_probe_enabled

    settings = get_settings()
    if runtime_paper_live_probe_enabled(db, settings):
        ...
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.db.models import SystemSetting


# 数据库 SystemSetting 表中的 key 常量
PAPER_LIVE_PROBE_ENABLED_KEY = "paper_live_probe_enabled"
PAPER_PROBE_MAX_NOTIONAL_KEY = "paper_probe_max_notional"
PAPER_PROBE_DAILY_MAX_RUNS_KEY = "paper_probe_daily_max_runs"
PAPER_PROBE_DAILY_MAX_NOTIONAL_KEY = "paper_probe_daily_max_notional"
PAPER_PROBE_COOLDOWN_MS_KEY = "paper_probe_cooldown_ms"
PAPER_PROBE_FLATTEN_TIMEOUT_SECONDS_KEY = "paper_probe_flatten_timeout_seconds"
PAPER_PROBE_MAKER_TIMEOUT_SECONDS_KEY = "paper_probe_maker_timeout_seconds"

PAPER_PROBE_DEFAULTS = {
    PAPER_PROBE_MAX_NOTIONAL_KEY: 20.0,
    PAPER_PROBE_DAILY_MAX_RUNS_KEY: 200,
    PAPER_PROBE_DAILY_MAX_NOTIONAL_KEY: 4000.0,
    PAPER_PROBE_COOLDOWN_MS_KEY: 500,
    PAPER_PROBE_FLATTEN_TIMEOUT_SECONDS_KEY: 20.0,
    PAPER_PROBE_MAKER_TIMEOUT_SECONDS_KEY: 5.0,
}


def execution_settings_payload(db: Session, settings: Settings) -> dict[str, Any]:
    """构建前端展示用的执行设置字典。

    参数:
        db: 数据库会话。
        settings: 应用根配置。

    返回:
        包含所有运行时执行设置的字典，供前端 API 返回。
    """
    return {
        "paper_live_probe_enabled": runtime_paper_live_probe_enabled_for_display(db, settings),
        "paper_live_probe_confirmation_required": "ENABLE PAPER LIVE PROBE",
        **paper_probe_limits(db),
    }


def set_execution_settings(
    db: Session,
    *,
    paper_live_probe_enabled: bool,
    paper_probe_max_notional: float = 20.0,
    paper_probe_daily_max_runs: int = 200,
    paper_probe_daily_max_notional: float = 4000.0,
    paper_probe_cooldown_ms: int = 500,
    paper_probe_flatten_timeout_seconds: float = 20.0,
    paper_probe_maker_timeout_seconds: float = 5.0,
) -> None:
    """将执行设置写入数据库。

    参数:
        db: 数据库会话。
        paper_live_probe_enabled: Paper-live 探针开关。
    """
    _set_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY, _bool_text(paper_live_probe_enabled))
    values = {
        PAPER_PROBE_MAX_NOTIONAL_KEY: paper_probe_max_notional,
        PAPER_PROBE_DAILY_MAX_RUNS_KEY: paper_probe_daily_max_runs,
        PAPER_PROBE_DAILY_MAX_NOTIONAL_KEY: paper_probe_daily_max_notional,
        PAPER_PROBE_COOLDOWN_MS_KEY: paper_probe_cooldown_ms,
        PAPER_PROBE_FLATTEN_TIMEOUT_SECONDS_KEY: paper_probe_flatten_timeout_seconds,
        PAPER_PROBE_MAKER_TIMEOUT_SECONDS_KEY: paper_probe_maker_timeout_seconds,
    }
    for key, value in values.items():
        _set_system_setting(db, key, str(value))


def paper_probe_limits(db: Session) -> dict[str, float | int]:
    """读取真实最小单探针的安全限额。"""
    result: dict[str, float | int] = {}
    integer_keys = {PAPER_PROBE_DAILY_MAX_RUNS_KEY, PAPER_PROBE_COOLDOWN_MS_KEY}
    for key, default in PAPER_PROBE_DEFAULTS.items():
        raw = _get_system_setting(db, key)
        try:
            result[key] = int(float(raw)) if key in integer_keys and raw is not None else (
                int(default) if key in integer_keys else float(raw) if raw is not None else float(default)
            )
        except (TypeError, ValueError):
            result[key] = int(default) if key in integer_keys else float(default)
    return result


def runtime_paper_live_probe_enabled(db: Session, settings: Settings) -> bool:
    """获取 Paper-live 探针运行时开关状态。

    优先读取数据库设置，未设置时回退到 ``settings.paper_live.probe_enabled``。

    参数:
        db: 数据库会话。
        settings: 应用根配置。

    返回:
        ``True`` 表示探针已启用。
    """
    value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
    if value is not None:
        return _parse_bool(value)
    return settings.paper_live.probe_enabled


def runtime_paper_live_probe_enabled_for_display(db: Session, settings: Settings) -> bool:
    """获取用于前端展示的 Paper-live 探针开关状态。

    与 ``runtime_paper_live_probe_enabled`` 使用同一数据库总开关。

    参数:
        db: 数据库会话。
        settings: 应用根配置。

    返回:
        ``True`` 表示前端应显示为已启用。
    """
    value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
    if value is not None:
        return _parse_bool(value)
    return bool(settings.paper_live.probe_enabled)


def paper_live_probe_enabled_for_venue(
    db: Session | None,
    settings: Settings,
    venue: str,
) -> bool:
    """判断指定 venue 的 Paper-live 探针是否启用。

    判断逻辑：
    1. MT5 不支持探针 → 直接返回 False
    2. 数据库有设置 → 使用数据库值
    3. Hyperliquid 有专属开关 → 返回 True
    4. 总开关未开启 → 返回 False
    5. 检查 venue 是否在允许列表中

    参数:
        db: 数据库会话（可为 None）。
        settings: 应用根配置。
        venue: 交易所标识（如 "hyperliquid" / "mt5" / "binance"）。

    返回:
        ``True`` 表示该 venue 的探针已启用。
    """
    venue = str(venue or "").strip().lower()
    if not venue or venue == "mt5":
        return False
    if db is not None:
        value = _get_system_setting(db, PAPER_LIVE_PROBE_ENABLED_KEY)
        if value is not None:
            return _parse_bool(value)
    if not settings.paper_live.probe_enabled:
        return False
    venues = _paper_live_probe_venues(settings)
    return "*" in venues or venue in venues


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _get_system_setting(db: Session, key: str) -> str | None:
    """从 SystemSetting 表读取指定 key 的值，不存在时返回 None。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row.value if row else None


def _set_system_setting(db: Session, key: str, value: str) -> None:
    """将指定 key 的值写入 / 更新到 SystemSetting 表。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first() or SystemSetting(key=key)
    row.value = value
    db.add(row)


def _parse_bool(value: str) -> bool:
    """将字符串解析为布尔值（支持 "1" / "true" / "yes" / "on"）。"""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _bool_text(value: bool) -> str:
    """将布尔值转换为数据库存储文本（"true" / "false"）。"""
    return "true" if value else "false"


def _paper_live_probe_venues(settings: Settings) -> set[str]:
    """从配置中解析允许探针的 venue 集合。

    读取 ``settings.paper_live.probe_venues``（逗号分隔字符串），
    返回小写化的 venue 集合。
    """
    raw = str(settings.paper_live.probe_venues or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}
