"""
MT5 交易时段模板与本地调度模块
==============================

管理 MT5 品种的交易时段模板（外汇、美股、加密货币、黄金等），
提供本地时段判定功能，在 MT5 终端不可用时作为兜底。

核心功能:
- ``apply_mt5_session_template``: 为品种应用预设时段模板
- ``local_schedule_state``: 根据本地时段配置判定当前交易状态
- ``infer_template``: 根据品种名自动推断模板类型
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import SymbolMapping

logger = get_logger(__name__)

# 星期常量
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_INDEX = {day: index for index, day in enumerate(DAYS)}


@dataclass(frozen=True)
class LocalScheduleState:
    """本地时段判定结果。

    属性:
        symbol: 品种名
        status: 状态标识（normal_trade / reduce_only / quote_only / closed）
        reason: 状态原因描述
        can_quote: 是否允许报价
        can_open_long: 是否允许开多
        can_open_short: 是否允许开空
        can_close_long: 是否允许平多
        can_close_short: 是否允许平空
        seconds_to_open: 距离下一开盘窗口的秒数
        seconds_to_close: 距离当前窗口关闭的秒数
        source: 数据来源标识
    """
    symbol: str
    status: str
    reason: str
    can_quote: bool
    can_open_long: bool
    can_open_short: bool
    can_close_long: bool
    can_close_short: bool
    seconds_to_open: int | None = None
    seconds_to_close: int | None = None
    source: str = "local_schedule"


def mt5_session_templates() -> list[dict[str, Any]]:
    """返回所有可用的 MT5 时段模板列表（供前端展示）。"""
    return [
        {"value": "auto", "label": "自动识别", "description": "按内部品种和 MT5 品种自动套用内置模板"},
        {"value": "manual_custom", "label": "手动 JSON", "description": "完全使用页面维护的 regular / close-only / quote-only JSON"},
        {"value": "always", "label": "全天交易", "description": "24x7，适合不需要 MT5 时段保护的品种"},
        {"value": "fx", "label": "外汇", "description": "周日 21:05 至周五 20:59 UTC"},
        {"value": "crypto_major", "label": "主流加密货币", "description": "24x7"},
        {"value": "stock_us_close_only", "label": "美股/类股票 close-only", "description": "盘前只允许平仓，开盘后才允许新增"},
        {"value": "index_us_jp", "label": "US30/USTEC/US500/JP225 指数", "description": "含日内小休和 close-only 缓冲"},
        {"value": "xauusd", "label": "XAUUSD 黄金", "description": "含每日短暂 close-only 保护"},
        {"value": "energy", "label": "能源", "description": "含每日休市保护"},
    ]


def apply_mt5_session_template(mapping: SymbolMapping, template: str | None = None) -> SymbolMapping:
    """为品种应用指定的时段模板。

    参数:
        mapping: 品种映射对象
        template: 模板名称，None 时使用 mapping 已有配置或 "auto"

    返回:
        已更新的 mapping 对象
    """
    selected = template or mapping.mt5_session_template or "auto"
    if selected == "auto":
        selected = infer_template(mapping)
    defaults = _template_payload(selected)
    mapping.mt5_session_enabled = True
    mapping.mt5_session_auto_sync = True
    mapping.mt5_session_template = selected
    mapping.mt5_session_timezone = defaults["timezone"]
    mapping.mt5_regular_sessions_json = _json(defaults["regular"])
    mapping.mt5_close_only_sessions_json = _json(defaults["close_only"])
    mapping.mt5_quote_only_sessions_json = _json(defaults["quote_only"])
    mapping.mt5_session_source = "exness_template"
    mapping.mt5_session_last_synced_at = utc_now()
    return mapping


def sync_mt5_session_templates(db: Session, *, only_auto: bool = True) -> int:
    """同步所有已启用品种的时段模板。

    参数:
        db: 数据库会话
        only_auto: 是否仅同步自动同步品种

    返回:
        同步的品种数量
    """
    query = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True))
    if only_auto:
        query = query.filter(SymbolMapping.mt5_session_auto_sync.is_(True))
    count = 0
    for mapping in query.all():
        template = mapping.mt5_session_template or "auto"
        if template == "manual_custom" and only_auto:
            continue
        apply_mt5_session_template(mapping, template)
        db.add(mapping)
        count += 1
    db.commit()
    return count


def infer_template(mapping: SymbolMapping) -> str:
    """根据品种名自动推断合适的时段模板。

    匹配规则（按优先级）：
    1. 美股代码 → stock_us_close_only
    2. 指数代码 → index_us_jp
    3. XAU/GOLD → xauusd
    4. OIL → energy
    5. 加密货币 → crypto_major
    6. 外汇对 → fx
    7. 其他 → manual_custom
    """
    text = f"{mapping.symbol} {mapping.mt5_symbol} {mapping.base_asset}".upper()
    compact = re.sub(r"[^A-Z0-9]", "", text)
    if any(token in compact for token in ("SPCX", "AAPL", "TSLA", "NVDA", "META", "AMZN", "MSFT", "GOOGL", "NFLX")):
        return "stock_us_close_only"
    if any(token in compact for token in ("JP225", "US30", "USTEC", "US500", "NAS100", "DJ30", "SPX500")):
        return "index_us_jp"
    if "XAU" in compact or "GOLD" in compact:
        return "xauusd"
    if any(token in compact for token in ("USOIL", "UKOIL", "OIL", "XNG")):
        return "energy"
    if any(token in compact for token in ("BTC", "ETH", "SOL", "HYPE")):
        return "crypto_major"
    if len(compact) >= 6 and any(quote in compact for quote in ("USD", "EUR", "JPY", "GBP", "AUD", "CAD", "CHF", "NZD")):
        return "fx"
    return "manual_custom"


def local_schedule_state(mapping: SymbolMapping, now: datetime | None = None) -> LocalScheduleState | None:
    """根据本地时段配置判定当前交易状态。

    参数:
        mapping: 品种映射对象
        now: 当前时间（None 时使用 UTC 时间）

    返回:
        LocalScheduleState 或 None（未启用时段管理时）
    """
    if not getattr(mapping, "mt5_session_enabled", True):
        return None
    template = getattr(mapping, "mt5_session_template", "auto") or "auto"
    regular = _loads(getattr(mapping, "mt5_regular_sessions_json", "[]"))
    close_only = _loads(getattr(mapping, "mt5_close_only_sessions_json", "[]"))
    quote_only = _loads(getattr(mapping, "mt5_quote_only_sessions_json", "[]"))
    # 自动模式且无配置时，不干预
    if template == "auto" and not regular and not close_only and not quote_only:
        return None
    # 非自动/手动模式且无配置时，使用模板默认值
    if template not in {"manual_custom", "auto"} and not regular and not close_only and not quote_only:
        defaults = _template_payload(template)
        regular = defaults["regular"]
        close_only = defaults["close_only"]
        quote_only = defaults["quote_only"]
    tz = _tz(getattr(mapping, "mt5_session_timezone", "UTC") or "UTC")
    current = _current_time(now, tz)

    # 优先检查 close-only 窗口
    close_match = _matching_window(close_only, current)
    if close_match:
        return LocalScheduleState(
            symbol=mapping.symbol,
            status="reduce_only",
            reason=f"本地 MT5 时段模板处于只平仓窗口: {close_match['label']}",
            can_quote=True,
            can_open_long=False,
            can_open_short=False,
            can_close_long=True,
            can_close_short=True,
            seconds_to_close=close_match["seconds_to_end"],
            source=_source(mapping),
        )
    # 检查 quote-only 窗口
    quote_match = _matching_window(quote_only, current)
    if quote_match:
        return LocalScheduleState(
            symbol=mapping.symbol,
            status="quote_only",
            reason=f"本地 MT5 时段模板处于仅报价窗口: {quote_match['label']}",
            can_quote=True,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            seconds_to_open=_seconds_to_next(regular, current),
            seconds_to_close=quote_match["seconds_to_end"],
            source=_source(mapping),
        )
    # 检查正常交易窗口
    regular_match = _matching_window(regular, current)
    if regular_match:
        return LocalScheduleState(
            symbol=mapping.symbol,
            status="normal_trade",
            reason=f"本地 MT5 时段模板处于正常交易窗口: {regular_match['label']}",
            can_quote=True,
            can_open_long=True,
            can_open_short=True,
            can_close_long=True,
            can_close_short=True,
            seconds_to_close=regular_match["seconds_to_end"],
            source=_source(mapping),
        )
    # 有配置但不在任何窗口内 → 休市
    if regular or close_only or quote_only:
        return LocalScheduleState(
            symbol=mapping.symbol,
            status="closed",
            reason="本地 MT5 时段模板判定当前休市",
            can_quote=False,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            seconds_to_open=_seconds_to_next(regular + close_only + quote_only, current),
            source=_source(mapping),
        )
    return None


def _template_payload(template: str) -> dict[str, Any]:
    """返回指定模板的默认时段配置。"""
    if template == "always" or template == "crypto_major":
        return {"timezone": "UTC", "regular": [{"days": list(DAYS), "start": "00:00", "end": "24:00"}], "close_only": [], "quote_only": []}
    if template == "fx":
        return {
            "timezone": "UTC",
            "regular": [{"days": ["sun"], "start": "21:05", "end": "24:00"}, {"days": ["mon", "tue", "wed", "thu"], "start": "00:00", "end": "24:00"}, {"days": ["fri"], "start": "00:00", "end": "20:59"}],
            "close_only": [],
            "quote_only": [],
        }
    if template == "stock_us_close_only":
        return {
            "timezone": "UTC",
            "regular": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "13:40", "end": "19:45"}],
            "close_only": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "10:00", "end": "13:40"}],
            "quote_only": [],
        }
    if template == "index_us_jp":
        return {
            "timezone": "UTC",
            "regular": [
                {"days": ["sun"], "start": "22:05", "end": "24:00"},
                {"days": ["mon", "tue", "wed", "thu"], "start": "00:00", "end": "20:59"},
                {"days": ["mon", "tue", "wed", "thu"], "start": "22:01", "end": "24:00"},
                {"days": ["fri"], "start": "00:00", "end": "20:55"},
            ],
            "close_only": [{"days": ["sun"], "start": "22:00", "end": "22:05"}, {"days": ["mon", "tue", "wed", "thu"], "start": "20:59", "end": "21:00"}, {"days": ["mon", "tue", "wed", "thu"], "start": "22:00", "end": "22:01"}],
            "quote_only": [{"days": ["mon", "tue", "wed", "thu"], "start": "21:00", "end": "22:00"}],
        }
    if template == "xauusd":
        return {
            "timezone": "UTC",
            "regular": [{"days": ["sun"], "start": "22:05", "end": "24:00"}, {"days": ["mon", "tue", "wed", "thu"], "start": "00:00", "end": "22:00"}, {"days": ["mon", "tue", "wed", "thu"], "start": "22:02", "end": "24:00"}, {"days": ["fri"], "start": "00:00", "end": "20:58"}],
            "close_only": [{"days": ["sun"], "start": "22:01:30", "end": "22:05"}, {"days": ["mon", "tue", "wed", "thu"], "start": "22:00:30", "end": "22:02"}],
            "quote_only": [{"days": ["mon", "tue", "wed", "thu"], "start": "20:58", "end": "22:00:30"}],
        }
    if template == "energy":
        return {
            "timezone": "UTC",
            "regular": [{"days": ["sun"], "start": "22:10", "end": "24:00"}, {"days": ["mon", "tue", "wed", "thu"], "start": "00:00", "end": "20:59"}, {"days": ["mon", "tue", "wed", "thu"], "start": "22:01", "end": "24:00"}, {"days": ["fri"], "start": "00:00", "end": "20:45"}],
            "close_only": [],
            "quote_only": [{"days": ["mon", "tue", "wed", "thu"], "start": "20:59", "end": "22:01"}],
        }
    return {"timezone": "UTC", "regular": [], "close_only": [], "quote_only": []}


def _matching_window(windows: list[dict[str, Any]], current: datetime) -> dict[str, Any] | None:
    """查找当前时间所在的时段窗口。"""
    for item in windows:
        for start, end in _window_datetimes(item, current):
            if start <= current < end:
                return {"label": _window_label(item), "seconds_to_end": int((end - current).total_seconds())}
    return None


def _seconds_to_next(windows: list[dict[str, Any]], current: datetime) -> int | None:
    """计算距离下一个时段窗口开始的秒数。"""
    candidates: list[int] = []
    for offset in range(8):
        pivot = current + timedelta(days=offset)
        for item in windows:
            for start, _ in _window_datetimes(item, pivot):
                if start > current:
                    candidates.append(int((start - current).total_seconds()))
    return min(candidates) if candidates else None


def _window_datetimes(item: dict[str, Any], current: datetime) -> list[tuple[datetime, datetime]]:
    """将时段配置项展开为具体的 datetime 窗口列表。"""
    days = [str(day).lower()[:3] for day in item.get("days", [])]
    if not days:
        days = list(DAYS)
    start_time = _parse_time(str(item.get("start", "00:00")))
    end_time = _parse_time(str(item.get("end", "24:00")))
    windows: list[tuple[datetime, datetime]] = []
    for day_delta in (-1, 0, 1):
        date = (current + timedelta(days=day_delta)).date()
        day_name = DAYS[date.weekday()]
        if day_name not in days:
            continue
        start = datetime.combine(date, start_time[0], tzinfo=current.tzinfo) + timedelta(days=start_time[1])
        end = datetime.combine(date, end_time[0], tzinfo=current.tzinfo) + timedelta(days=end_time[1])
        if end <= start:
            end += timedelta(days=1)
        windows.append((start, end))
    return windows


def _parse_time(value: str) -> tuple[time, int]:
    """解析时间字符串，支持 ``"24:00"`` 特殊格式（返回 day_offset=1）。"""
    if value == "24:00":
        return time(0, 0), 1
    parts = [int(part) for part in value.split(":")]
    while len(parts) < 3:
        parts.append(0)
    return time(parts[0], parts[1], parts[2]), 0


def _current_time(now: datetime | None, tz: ZoneInfo | timezone) -> datetime:
    """获取指定时区的当前时间。"""
    if now is None:
        return datetime.now(timezone.utc).astimezone(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc).astimezone(tz)
    return now.astimezone(tz)


def _loads(value: str | None) -> list[dict[str, Any]]:
    """安全解析 JSON 数组字符串。"""
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json(value: list[dict[str, Any]]) -> str:
    """将时段配置序列化为紧凑 JSON。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tz(value: str) -> ZoneInfo | timezone:
    """将时区字符串转换为 ZoneInfo 或 timezone 对象。"""
    if value.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(value)
    except Exception:
        return timezone.utc


def _window_label(item: dict[str, Any]) -> str:
    """生成时段窗口的可读标签。"""
    return f"{','.join(item.get('days', []))} {item.get('start', '')}-{item.get('end', '')}"


def _source(mapping: SymbolMapping) -> str:
    """获取品种的时段数据来源标识。"""
    return getattr(mapping, "mt5_session_source", "") or "local_schedule"
