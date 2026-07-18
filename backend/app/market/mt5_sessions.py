"""
MT5 会话状态管理模块
====================

判定每个 MT5 品种当前是否可报价、可开仓、可平仓。

数据来源优先级:
1. 本地时段模板（``mt5_schedule.local_schedule_state``）
2. MT5 Gateway 的 tick 新鲜度 + trade_mode 兜底

使用 ``TTLCache`` 替代手写 dict+Lock+TTL 缓存。

使用方式::

    from app.market.mt5_sessions import mt5_session_state, mt5_action_allowed

    state = mt5_session_state(mapping)
    if state.can_quote:
        allowed, reason = mt5_action_allowed(state, direction, "open")
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from app.config.settings import get_settings
from app.core.cache import TTLCache
from app.core.logging import get_logger
from app.db.models import SymbolMapping
from app.market.mt5_schedule import LocalScheduleState, local_schedule_state

logger = get_logger(__name__)


@dataclass(frozen=True)
class MT5SessionState:
    """MT5 会话状态快照。

    属性:
        symbol: 品种名
        status: 状态标识（normal_trade / reduce_only / quote_only / closed / unknown）
        reason: 状态原因描述
        can_quote: 是否允许报价
        can_open_long: 是否允许开多
        can_open_short: 是否允许开空
        can_close_long: 是否允许平多
        can_close_short: 是否允许平空
        seconds_to_open: 距下一开盘秒数
        seconds_to_close: 距当前窗口关闭秒数
        trade_mode: MT5 交易模式名称
        session_source: 数据来源（mt5_session / mt5_tick_trade_mode_fallback / paper / fallback）
        mt5_leg: MT5 对应的腿（"a" 或 "b"）
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
    trade_mode: str = "unknown"
    session_source: str = "fallback"
    mt5_leg: str = "b"

    @property
    def can_open_any(self) -> bool:
        return self.can_open_long or self.can_open_short

    @property
    def can_close_any(self) -> bool:
        return self.can_close_long or self.can_close_short


# 会话状态缓存：使用 TTLCache 替代手写 dict+monotonic+Lock
_session_cache: TTLCache[MT5SessionState] = TTLCache(ttl_seconds=30.0, namespace="mt5-sessions")


def _mt5_leg(mapping: SymbolMapping) -> str:
    """判断 MT5 对应的是 A 腿还是 B 腿。"""
    if str(getattr(mapping, "leg_a_venue", "") or "").strip().lower() == "mt5":
        return "a"
    return "b"


def _direction_is_mt5_long(direction: str, mt5_leg: str = "b") -> bool:
    """判断指定方向是否为 MT5 做多。"""
    if direction == "long_mt5_short_hyperliquid":
        return True
    if direction == "long_hyperliquid_short_mt5":
        return False
    if direction == "long_leg_a_short_leg_b":
        return mt5_leg == "a"
    if direction == "long_leg_b_short_leg_a":
        return mt5_leg == "b"
    return False


def mt5_session_state(mapping: SymbolMapping, now: datetime | None = None) -> MT5SessionState:
    """获取品种的 MT5 会话状态。

    参数:
        mapping: 品种映射对象
        now: 当前时间（None 时使用 datetime.now()）

    返回:
        MT5SessionState
    """
    current = now or datetime.now()
    mt5_leg = _mt5_leg(mapping)
    # 优先使用本地时段模板
    local_state = local_schedule_state(mapping, now)
    if local_state and local_state.status != "normal_trade":
        return _from_local_schedule(local_state, mt5_leg)
    settings = get_settings()
    # Paper 模式默认可交易
    if settings.quote.source_mode != "live":
        return MT5SessionState(
            symbol=mapping.symbol,
            status="normal_trade",
            reason="paper 模式默认可交易",
            can_quote=True,
            can_open_long=True,
            can_open_short=True,
            can_close_long=True,
            can_close_short=True,
            trade_mode="paper",
            session_source="paper",
            mt5_leg=mt5_leg,
        )
    cache_key = f"session_{mapping.id or hash(mapping.mt5_symbol)}"
    cached = _session_cache.get(cache_key)
    if cached:
        return cached
    try:
        from app.venues.manager import native_venue_manager
        connector = native_venue_manager.connector_for("mt5", "live")
        info = connector.get_instrument(mapping.mt5_symbol)
        tick = connector.get_ticker(mapping.mt5_symbol)
        return _remember_session(cache_key, _gateway_tick_state(mapping, info.raw, tick, current))
    except Exception as exc:
        return _remember_session(cache_key, _fallback_closed(mapping, f"MT5 Gateway 会话读取失败: {exc}"))


def _gateway_tick_state(mapping: SymbolMapping, info: dict[str, Any], tick: Any, current: datetime) -> MT5SessionState:
    """使用 Gateway 的品种状态和 tick 新鲜度进行会话兜底。"""
    trade_mode_value = int(info.get("trade_mode", -1))
    names = {0: "disabled", 1: "long_only", 2: "short_only", 3: "close_only", 4: "full"}
    trade_mode = names.get(trade_mode_value, f"unknown:{trade_mode_value}")
    permissions = {
        "disabled": (False, False, False, False), "close_only": (False, False, True, True),
        "long_only": (True, False, True, True), "short_only": (False, True, True, True),
        "full": (True, True, True, True),
    }.get(trade_mode, (False, False, False, False))
    can_open_long, can_open_short, can_close_long, can_close_short = permissions
    exchange_time = tick.exchange_time or tick.received_at
    tick_age = (current - exchange_time).total_seconds()
    if float(tick.bid) <= 0 or float(tick.ask) <= 0 or tick_age > get_settings().mt5.session_tick_stale_seconds:
        return _fallback_closed(mapping, f"MT5 Gateway tick 不可用或已过期 {int(tick_age)} 秒")
    status = "normal_trade" if can_open_long or can_open_short else "reduce_only"
    return MT5SessionState(
        symbol=mapping.symbol, status=status,
        reason="MT5 Gateway tick 与 trade_mode 检查通过" if status == "normal_trade" else "MT5 当前仅允许平仓",
        can_quote=True, can_open_long=can_open_long, can_open_short=can_open_short,
        can_close_long=can_close_long, can_close_short=can_close_short,
        trade_mode=trade_mode, session_source="mt5_gateway", mt5_leg=_mt5_leg(mapping),
    )


def mt5_action_allowed(state: MT5SessionState, direction: str, action: str) -> tuple[bool, str]:
    """判断指定方向和动作是否被允许。

    参数:
        state: MT5 会话状态
        direction: 方向常量
        action: 动作（``"open"`` / ``"close"``）

    返回:
        (allowed, reason) 元组
    """
    mt5_long = _direction_is_mt5_long(direction, state.mt5_leg)
    if action == "open":
        allowed = state.can_open_long if mt5_long else state.can_open_short
        if allowed:
            return True, ""
        return False, f"MT5 当前不允许该方向新开仓: {state.status}，{state.reason}"
    if action == "close":
        allowed = state.can_close_long if mt5_long else state.can_close_short
        if allowed:
            return True, ""
        return False, f"MT5 当前不允许该方向平仓: {state.status}，{state.reason}"
    return False, "未知 MT5 动作"


def as_session_dict(state: MT5SessionState) -> dict[str, Any]:
    """将会话状态转为字典（用于 API 响应）。"""
    return {
        "symbol": state.symbol,
        "status": state.status,
        "reason": state.reason,
        "can_quote": state.can_quote,
        "can_open_long": state.can_open_long,
        "can_open_short": state.can_open_short,
        "can_close_long": state.can_close_long,
        "can_close_short": state.can_close_short,
        "seconds_to_open": state.seconds_to_open,
        "seconds_to_close": state.seconds_to_close,
        "trade_mode": state.trade_mode,
        "session_source": state.session_source,
        "mt5_leg": state.mt5_leg,
    }


def _from_local_schedule(state: LocalScheduleState, mt5_leg: str = "b") -> MT5SessionState:
    """将本地时段状态转换为 MT5SessionState。"""
    return MT5SessionState(
        symbol=state.symbol,
        status=state.status,
        reason=state.reason,
        can_quote=state.can_quote,
        can_open_long=state.can_open_long,
        can_open_short=state.can_open_short,
        can_close_long=state.can_close_long,
        can_close_short=state.can_close_short,
        seconds_to_open=state.seconds_to_open,
        seconds_to_close=state.seconds_to_close,
        trade_mode=state.status,
        session_source=state.source,
        mt5_leg=mt5_leg,
    )


def _read_sessions(mt5: Any, symbol: str, current: datetime, kind: str) -> list[tuple[datetime, datetime]]:
    """读取 MT5 交易/报价时段窗口列表。"""
    weekday = current.isoweekday() % 7
    reader = mt5.symbol_info_session_trade if kind == "trade" else mt5.symbol_info_session_quote
    windows: list[tuple[datetime, datetime]] = []
    index = 0
    while index < 32:
        session = reader(symbol, weekday, index)
        if not session:
            break
        start_raw = getattr(session, "from", None) or getattr(session, "from_", None) or session[0]
        end_raw = getattr(session, "to", None) or session[1]
        start_time = _to_time(start_raw)
        end_time = _to_time(end_raw)
        start_dt = datetime.combine(current.date(), start_time)
        end_dt = datetime.combine(current.date(), end_time)
        if end_dt <= start_dt:
            end_dt += timedelta(days=1)
        windows.append((start_dt, end_dt))
        index += 1
    return windows


def _fallback_from_tick(mt5: Any, mapping: SymbolMapping, info: Any, current: datetime) -> MT5SessionState:
    """当 MT5 不支持 session API 时，使用 tick 新鲜度和 trade_mode 兜底。"""
    settings = get_settings()
    mt5_leg = _mt5_leg(mapping)
    tick = mt5.symbol_info_tick(mapping.mt5_symbol)
    trade_mode = _trade_mode_name(mt5, int(getattr(info, "trade_mode", -1)))
    can_open_long, can_open_short, can_close_long, can_close_short = _permissions_from_trade_mode(
        mt5, trade_mode, int(getattr(info, "trade_mode", -1)),
    )
    if not tick or float(getattr(tick, "bid", 0.0) or 0.0) <= 0 or float(getattr(tick, "ask", 0.0) or 0.0) <= 0:
        return MT5SessionState(
            symbol=mapping.symbol,
            status="closed",
            reason="当前 MT5 tick 不可用，按不可交易处理",
            can_quote=False,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            trade_mode=trade_mode,
            session_source="mt5_tick_trade_mode_fallback",
            mt5_leg=mt5_leg,
        )
    tick_seconds = getattr(tick, "time_msc", 0)
    tick_time = datetime.fromtimestamp(tick_seconds / 1000) if tick_seconds else datetime.fromtimestamp(getattr(tick, "time", 0))
    tick_age = (current - tick_time).total_seconds()
    if tick_age > settings.mt5.session_tick_stale_seconds:
        return MT5SessionState(
            symbol=mapping.symbol,
            status="closed",
            reason=f"MT5 tick 已 {int(tick_age)} 秒未更新，按休市或不可交易处理",
            can_quote=False,
            can_open_long=False,
            can_open_short=False,
            can_close_long=False,
            can_close_short=False,
            trade_mode=trade_mode,
            session_source="mt5_tick_trade_mode_fallback",
            mt5_leg=mt5_leg,
        )
    status = "normal_trade"
    reason = "MT5 Python 包不支持 session API，使用 tick 新鲜度和 trade_mode 兜底"
    if trade_mode == "close_only":
        status = "reduce_only"
        reason = "MT5 当前 trade_mode 为只平仓"
    elif not can_open_long and not can_open_short:
        status = "reduce_only"
        reason = "MT5 当前 trade_mode 不允许新开仓"
    return MT5SessionState(
        symbol=mapping.symbol,
        status=status,
        reason=reason,
        can_quote=True,
        can_open_long=can_open_long,
        can_open_short=can_open_short,
        can_close_long=can_close_long,
        can_close_short=can_close_short,
        trade_mode=trade_mode,
        session_source="mt5_tick_trade_mode_fallback",
        mt5_leg=mt5_leg,
    )


def _to_time(value: Any) -> time:
    """将各种格式的时间值转换为 datetime.time。"""
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, int):
        return (datetime.min + timedelta(seconds=value)).time()
    text = str(value)
    if ":" in text:
        parts = [int(part) for part in text.split(":")[:3]]
        while len(parts) < 3:
            parts.append(0)
        return time(parts[0], parts[1], parts[2])
    return time(0, 0)


def _window_state(windows: list[tuple[datetime, datetime]], current: datetime) -> tuple[bool, int | None, int | None]:
    """判断当前时间是否在时段窗口内，并计算到开/关的秒数。"""
    if not windows:
        return True, None, None
    next_open: int | None = None
    for start, end in windows:
        if start <= current <= end:
            return True, int((end - current).total_seconds()), int((current - start).total_seconds())
        if current < start:
            seconds = int((start - current).total_seconds())
            next_open = seconds if next_open is None else min(next_open, seconds)
    if next_open is None:
        first_start = min(start for start, _ in windows) + timedelta(days=1)
        next_open = int((first_start - current).total_seconds())
    return False, None, next_open


def _trade_mode_name(mt5: Any, trade_mode: int) -> str:
    """将 MT5 trade_mode 整数映射为可读名称。"""
    names = {
        getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", -100): "disabled",
        getattr(mt5, "SYMBOL_TRADE_MODE_LONGONLY", -101): "long_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_SHORTONLY", -102): "short_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_CLOSEONLY", -103): "close_only",
        getattr(mt5, "SYMBOL_TRADE_MODE_FULL", -104): "full",
    }
    return names.get(trade_mode, f"unknown:{trade_mode}")


def _permissions_from_trade_mode(mt5: Any, trade_mode_name: str, trade_mode: int) -> tuple[bool, bool, bool, bool]:
    """根据 trade_mode 名称返回 (can_open_long, can_open_short, can_close_long, can_close_short)。"""
    if trade_mode_name == "disabled":
        return False, False, False, False
    if trade_mode_name == "close_only":
        return False, False, True, True
    if trade_mode_name == "long_only":
        return True, False, True, True
    if trade_mode_name == "short_only":
        return False, True, True, True
    if trade_mode_name == "full":
        return True, True, True, True
    if trade_mode >= 0:
        return True, True, True, True
    return False, False, False, False


def _fallback_closed(mapping: SymbolMapping, reason: str) -> MT5SessionState:
    """构造一个不可交易的兜底状态。"""
    return MT5SessionState(
        symbol=mapping.symbol,
        status="unknown",
        reason=reason,
        can_quote=False,
        can_open_long=False,
        can_open_short=False,
        can_close_long=False,
        can_close_short=False,
        trade_mode="unknown",
        session_source="fallback",
        mt5_leg=_mt5_leg(mapping),
    )


def _remember_session(cache_key: str, state: MT5SessionState) -> MT5SessionState:
    """将状态写入 TTLCache 并返回。"""
    _session_cache.set(cache_key, state)
    return state
