"""
MT5 交易能力缓存模块
====================

管理 MT5 品种的交易能力状态（是否允许买入/卖出），
通过 ``mt5_market_order_check`` 预检订单，并缓存结果。

核心类 ``MT5TradabilityCache``:
- 缓存每个品种×方向的交易能力状态
- 支持交易拒绝隔离（quarantine）机制
- 支持持久化隔离记录（写入 SystemSetting 表）
- 后台定期刷新缓存

使用方式::

    from app.market.mt5_tradability import mt5_tradability_cache

    allowed, reason = mt5_tradability_cache.is_fresh_allowed("BTCUSD", "buy")
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time

from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from app.adapters.mt5 import MT5OrderCheck, mt5_market_order_check
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import StrategySetting, SystemSetting
from app.market.symbols import enabled_mappings

logger = get_logger(__name__)


@dataclass(frozen=True)
class TradabilityState:
    """交易能力状态快照。

    属性:
        symbol: 品种名
        mt5_symbol: MT5 品种名
        side: 方向（buy / sell）
        allowed: 是否允许交易
        message: 状态描述
        checked_at: 检查时间戳（time.time()）
        quantity: 检查时使用的数量
        retcode: MT5 返回码
        source: 数据来源
    """
    symbol: str
    mt5_symbol: str
    side: str
    allowed: bool
    message: str
    checked_at: float
    quantity: float
    retcode: int | None = None
    source: str = "unknown"

    @property
    def age_ms(self) -> float:
        """缓存年龄（毫秒）。"""
        return (time.time() - self.checked_at) * 1000


class MT5TradabilityCache:
    """MT5 交易能力缓存管理器。

    线程安全，内部使用 Lock 保护共享状态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 品种×方向 → 最新交易能力状态
        self._states: dict[tuple[str, str], TradabilityState] = {}
        # 品种×方向 → (隔离截止时间, 隔离原因)
        self._blocked: dict[tuple[str, str], tuple[float, str]] = {}
        self._initialized = False
        self._last_refresh_at = 0.0

    def initialized(self) -> bool:
        """缓存是否已完成首次刷新。"""
        with self._lock:
            return self._initialized

    def mark_not_initialized(self) -> None:
        """标记缓存未初始化（例如 MT5 断开连接时）。"""
        with self._lock:
            self._initialized = False

    def get(self, symbol: str, side: str) -> TradabilityState | None:
        """获取指定品种×方向的缓存状态。"""
        key = (symbol.upper(), side.lower())
        with self._lock:
            return self._states.get(key)

    def is_fresh_allowed(self, symbol: str, side: str, ttl_ms: int | None = None) -> tuple[bool, str]:
        """检查交易能力是否新鲜且允许。

        参数:
            symbol: 品种名
            side: 方向（buy / sell）
            ttl_ms: 最大缓存年龄（毫秒），None 时使用配置值

        返回:
            (allowed, reason) 元组
        """
        blocked = self._active_block(symbol, side)
        if blocked:
            return False, blocked
        state = self.get(symbol, side)
        if not state:
            return False, "MT5 交易能力缓存缺失"
        max_age = ttl_ms if ttl_ms is not None else get_settings().mt5.tradability_cache_ttl_ms
        if state.age_ms > max_age:
            return False, f"MT5 交易能力缓存过期: {state.age_ms:.0f}ms > {max_age}ms"
        if not state.allowed:
            return False, state.message
        return True, ""

    def update(self, symbol: str, mt5_symbol: str, side: str, quantity: float, check: MT5OrderCheck, source: str) -> TradabilityState:
        """更新交易能力状态。

        如果品种处于隔离期，即使 check 通过也会被标记为不允许。
        """
        blocked = self._active_block(symbol, side)
        allowed = check.allowed and not blocked
        message = blocked or check.message
        state = TradabilityState(
            symbol=symbol.upper(),
            mt5_symbol=mt5_symbol,
            side=side.lower(),
            allowed=allowed,
            message=message,
            checked_at=time.time(),
            quantity=quantity,
            retcode=getattr(check, "retcode", None),
            source=source,
        )
        with self._lock:
            self._states[(state.symbol, state.side)] = state
        return state

    def refresh(self, db: Session) -> dict[str, int]:
        """刷新所有已启用品种的交易能力缓存。

        返回:
            {"checked": 检查总数, "allowed": 允许数}
        """
        self.load_persistent_blocks(db)
        strategy = db.query(StrategySetting).first() or StrategySetting()
        simulated = strategy.execution_mode == "paper"
        checked = 0
        allowed = 0
        for mapping in enabled_mappings(db):
            quantity = _probe_quantity(mapping)
            for side in ("buy", "sell"):
                check = mt5_market_order_check(mapping.mt5_symbol, side, quantity, demo=simulated)
                state = self.update(mapping.symbol, mapping.mt5_symbol, side, quantity, check, "background")
                checked += 1
                if state.allowed:
                    allowed += 1
        with self._lock:
            self._initialized = True
            self._last_refresh_at = time.time()
        logger.info("MT5 交易能力缓存刷新完成: checked={}, allowed={}", checked, allowed)
        return {"checked": checked, "allowed": allowed}

    def block(
        self,
        db: Session,
        symbol: str,
        mt5_symbol: str,
        side: str,
        quantity: float,
        message: str,
        *,
        seconds: int | None = None,
        source: str = "runtime_reject",
    ) -> TradabilityState:
        """将品种×方向加入隔离期。

        参数:
            db: 数据库会话
            symbol: 品种名
            mt5_symbol: MT5 品种名
            side: 方向
            quantity: 交易数量
            message: 隔离原因
            seconds: 隔离时长（秒），None 时使用配置值
            source: 来源标识

        返回:
            更新后的 TradabilityState
        """
        duration = seconds if seconds is not None else get_settings().mt5.trade_reject_quarantine_seconds
        until = time.time() + max(duration, 1)
        key = (symbol.upper(), side.lower())
        with self._lock:
            self._blocked[key] = (until, message)
        _persist_block(db, key[0], key[1], mt5_symbol, quantity, message, until)
        return self.update(symbol, mt5_symbol, side, quantity, MT5OrderCheck(False, message), source)

    def load_persistent_blocks(self, db: Session) -> None:
        """从数据库加载持久化的隔离记录。"""
        now = time.time()
        rows = db.query(SystemSetting).filter(SystemSetting.key.like("mt5_tradability_block:%")).all()
        active: dict[tuple[str, str], tuple[float, str]] = {}
        stale_keys: list[str] = []
        for row in rows:
            try:
                payload = json.loads(row.value or "{}")
                until = float(payload.get("until", 0.0) or 0.0)
                message = str(payload.get("message", "") or "MT5 交易方向临时隔离")
                _, symbol, side = row.key.split(":", 2)
            except Exception:
                stale_keys.append(row.key)
                continue
            if until <= now:
                stale_keys.append(row.key)
                continue
            active[(symbol.upper(), side.lower())] = (until, message)
        with self._lock:
            self._blocked.update(active)
        # 清理过期的隔离记录
        for key in stale_keys:
            db.query(SystemSetting).filter(SystemSetting.key == key).delete()
        if stale_keys:
            try:
                db.commit()
            except OperationalError:
                db.rollback()

    def snapshot(self) -> list[dict]:
        """返回当前缓存快照（用于诊断页面展示）。"""
        with self._lock:
            states = list(self._states.values())
            initialized = self._initialized
            last_refresh_at = self._last_refresh_at
        return [
            {
                "symbol": state.symbol,
                "mt5_symbol": state.mt5_symbol,
                "side": state.side,
                "allowed": state.allowed,
                "message": state.message,
                "age_ms": state.age_ms,
                "quantity": state.quantity,
                "retcode": state.retcode,
                "source": state.source,
                "initialized": initialized,
                "last_refresh_at": last_refresh_at,
            }
            for state in states
        ]

    def _active_block(self, symbol: str, side: str) -> str:
        """检查品种×方向是否处于隔离期，返回隔离原因或空字符串。"""
        key = (symbol.upper(), side.lower())
        with self._lock:
            blocked = self._blocked.get(key)
            if not blocked:
                return ""
            until, message = blocked
            if until <= time.time():
                self._blocked.pop(key, None)
                return ""
            remaining = until - time.time()
            return f"{message}; quarantine_remaining={remaining:.0f}s"


def _probe_quantity(mapping) -> float:
    """计算交易能力探测使用的最小手数。"""
    lot_min = float(mapping.mt5_min_lot or mapping.min_order_size or 0.01)
    if lot_min <= 0:
        return 0.01
    return lot_min


# 全局单例
mt5_tradability_cache = MT5TradabilityCache()


def refresh_mt5_tradability_cache(db: Session) -> dict[str, int]:
    """刷新 MT5 交易能力缓存（模块级便捷函数）。"""
    return mt5_tradability_cache.refresh(db)


def block_mt5_tradability(
    db: Session,
    symbol: str,
    mt5_symbol: str,
    side: str,
    quantity: float,
    message: str,
    *,
    seconds: int | None = None,
    source: str = "runtime_reject",
) -> TradabilityState:
    """将品种加入交易隔离期（模块级便捷函数）。"""
    return mt5_tradability_cache.block(db, symbol, mt5_symbol, side, quantity, message, seconds=seconds, source=source)


def _persist_block(db: Session, symbol: str, side: str, mt5_symbol: str, quantity: float, message: str, until: float) -> None:
    """将隔离记录持久化到 SystemSetting 表。"""
    key = f"mt5_tradability_block:{symbol.upper()}:{side.lower()}"
    row = db.get(SystemSetting, key)
    if not row:
        row = SystemSetting(key=key)
        db.add(row)
    row.value = json.dumps(
        {
            "symbol": symbol.upper(),
            "side": side.lower(),
            "mt5_symbol": mt5_symbol,
            "quantity": quantity,
            "message": message,
            "until": until,
            "blocked_at": utc_now().isoformat(),
        },
        ensure_ascii=False,
    )
