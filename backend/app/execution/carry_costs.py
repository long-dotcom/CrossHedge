"""
资金费 / 过夜费同步模块
========================

定期从 Hyperliquid API 和 MT5 终端获取持仓的资金费率（funding）
和过夜费（swap），并更新到对冲组记录中。

数据来源：
- **Hyperliquid funding**：
  - live 模式：通过 ``userFunding`` 接口获取用户实际资金费
  - paper 模式：通过 ``fundingHistory`` 接口 × 名义价值估算
- **MT5 swap**：
  - 优先从当前持仓的 ``swap`` 字段读取
  - 回退到历史成交（``history_deals_get``）中累加

使用 ``post_hyperliquid_info`` 统一 HTTP 调用，
MT5 数据统一从独立 Gateway 读取，
使用 ``run_worker`` 模板自动记录 WorkerRun 和执行 prune。

使用方式::

    from app.core.db_session import db_session
    from app.execution.carry_costs import run_carry_cost_sync

    with db_session() as db:
        count = run_carry_cost_sync(db)
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.config.settings import get_settings
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import HedgeGroup, Order, SymbolMapping, SystemLog, WorkerRun
from app.execution.hedge_pool import hedge_pool
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)

# 视为"活跃"的对冲组状态
ACTIVE_COST_STATUSES = {"open", "open_partial", "closing", "manual_intervention"}

# 上次同步时间戳（用于节流）
_last_sync_at = 0.0


def run_carry_cost_sync(db: Session, *, force: bool = False) -> int:
    """资金费/过夜费同步入口。

    使用 ``run_worker`` 模板包装，自动记录 WorkerRun 并执行 prune。
    内置节流机制：默认间隔内不会重复同步。

    参数:
        db: 数据库会话。
        force: 是否强制同步（忽略节流间隔）。

    返回:
        发生变化的对冲组数量。
    """
    return run_worker(
        db,
        "carry_cost_sync",
        lambda db: _carry_cost_impl(db, force=force),
        prune_models=[WorkerRun, SystemLog],
    )


def _carry_cost_impl(db: Session, *, force: bool = False) -> int:
    """资金费/过夜费同步内部实现。"""
    global _last_sync_at
    settings = get_settings()
    now = time.time()
    if not force and now - _last_sync_at < max(settings.cost.carry_cost_sync_interval_seconds, 1):
        return 0
    _last_sync_at = now
    changed = 0
    groups = db.query(HedgeGroup).filter(HedgeGroup.status.in_(ACTIVE_COST_STATUSES)).all()
    for group in groups:
        mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
        if not mapping:
            continue
        old_funding = float(group.funding or 0.0)
        old_swap = float(group.swap or 0.0)
        funding = _hyperliquid_funding_cost(group, mapping) if _venue_leg(mapping, "hyperliquid") else None
        swap = _mt5_swap_cost(db, group, mapping) if _venue_leg(mapping, "mt5") else None
        if funding is not None:
            group.funding = funding
        if swap is not None:
            group.swap = swap
        if abs(float(group.funding or 0.0) - old_funding) > 1e-9 or abs(float(group.swap or 0.0) - old_swap) > 1e-9:
            changed += 1
    # 同步完成后刷新 Redis 对冲组快照
    hedge_pool.load_from_db(db)
    return changed


def _hyperliquid_funding_cost(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    """获取 Hyperliquid 侧的资金费。

    live 模式读取用户实际资金费，paper 模式根据历史费率估算。
    """
    if not group.opened_at:
        return None
    if group.execution_mode == "live":
        amount = _hyperliquid_user_funding_usdc(group, mapping)
        if amount is not None:
            return -amount
    return _paper_hyperliquid_funding_cost(group, mapping)


def _hyperliquid_user_funding_usdc(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    """通过 Hyperliquid userFunding 接口获取用户实际资金费（USDC）。"""
    settings = get_settings()
    user = settings.hyperliquid.account_address
    if not user:
        return None
    start_ms, end_ms = _group_window_ms(group)
    payload = {"type": "userFunding", "user": user, "startTime": start_ms, "endTime": end_ms}
    try:
        rows = post_hyperliquid_info(settings.hyperliquid.info_url, payload)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    symbols = _hyperliquid_symbol_aliases(mapping)
    if not symbols:
        return None
    total = 0.0
    for row in rows:
        delta = row.get("delta", {}) if isinstance(row, dict) else {}
        coin = str(delta.get("coin") or "")
        if coin not in symbols:
            continue
        total += _float(delta.get("usdc"))
    return total


def _paper_hyperliquid_funding_cost(group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    """Paper 模式下根据 fundingHistory 估算资金费。"""
    hyper_leg = _venue_leg(mapping, "hyperliquid")
    if not hyper_leg:
        return None
    _, hyper_symbol = hyper_leg
    start_ms, end_ms = _group_window_ms(group)
    if end_ms <= start_ms:
        return 0.0
    try:
        settings = get_settings()
        rows = post_hyperliquid_info(
            settings.hyperliquid.info_url,
            {"type": "fundingHistory", "coin": hyper_symbol, "startTime": start_ms, "endTime": end_ms},
        )
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    side_sign = 1.0 if _direction_is_venue_long(group.direction, hyper_leg[0]) else -1.0
    notional = float(group.notional or 0.0)
    return sum(notional * _float(row.get("fundingRate")) * side_sign for row in rows if isinstance(row, dict))


def _mt5_swap_cost(db: Session, group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    """获取 MT5 侧的过夜费（swap）。

    通过 Redis 代理连接器读取 Gateway 快照和成交数据。
    """
    if not group.opened_at:
        return None
    try:
        connector = native_venue_manager.connector_for("mt5", "live")
    except Exception:
        return None
    if group.status in ACTIVE_COST_STATUSES:
        position_swap = _open_position_swap(connector, group, mapping)
        if position_swap is not None:
            return -position_swap
    deal_swap = _deal_swap(db, connector, group)
    if deal_swap is not None:
        return -deal_swap
    return None


def _open_position_swap(connector, group: HedgeGroup, mapping: SymbolMapping) -> float | None:
    """从当前 MT5 持仓中读取 swap 值。"""
    mt5_leg = _venue_leg(mapping, "mt5")
    if not mt5_leg:
        return None
    mt5_leg_name, mt5_symbol = mt5_leg
    try:
        positions = connector.get_positions()
    except Exception:
        return None
    target_side = "LONG" if _direction_is_venue_long(group.direction, mt5_leg_name) else "SHORT"
    candidates = [
        position
        for position in positions or []
        if position.symbol == mt5_symbol
        and position.position_side.value == target_side
        and float(position.quantity) > 0
    ]
    if not candidates:
        return None
    total_volume = sum(float(position.quantity) for position in candidates)
    total_swap = sum(float(position.raw.get("swap", 0.0) or 0.0) for position in candidates)
    if total_volume <= 0:
        return None
    expected_quantity = group.leg_a_quantity if mt5_leg_name == "a" else group.leg_b_quantity
    expected = float(expected_quantity or group.quantity or 0.0)
    ratio = min(max(expected / total_volume, 0.0), 1.0) if expected > 0 else 1.0
    return total_swap * ratio


def _deal_swap(db: Session, connector, group: HedgeGroup) -> float | None:
    """从 MT5 历史成交中累加 swap 值（回退方案）。"""
    orders = db.query(Order).filter(Order.hedge_group_id == group.id, Order.platform == "mt5", Order.external_order_id != "").all()
    total = 0.0
    found = False
    for order in orders:
        try:
            ticket = int(str(order.external_order_id).strip())
        except (TypeError, ValueError):
            continue
        try:
            rows = connector.get_fills(venue_order_id=str(ticket))
        except Exception:
            rows = []
        for deal in rows:
            found = True
            total += float(deal.raw.get("swap", 0.0) or 0.0)
    return total if found else None


def _group_window_ms(group: HedgeGroup) -> tuple[int, int]:
    """计算对冲组的起止时间窗口（毫秒时间戳）。"""
    start = group.opened_at or group.created_at
    end = group.closed_at or utc_now()
    return int(start.timestamp() * 1000), int((end + timedelta(seconds=1)).timestamp() * 1000)


def _hyperliquid_symbol_aliases(mapping: SymbolMapping) -> set[str]:
    """获取 Hyperliquid 品种名称的所有别名（含冒号后部分）。"""
    hyper_leg = _venue_leg(mapping, "hyperliquid")
    if not hyper_leg:
        return set()
    value = str(hyper_leg[1] or "")
    aliases = {value}
    if ":" in value:
        aliases.add(value.split(":", 1)[1])
    return aliases


def _venue_leg(mapping: SymbolMapping, venue: str) -> tuple[str, str] | None:
    """查找指定 venue 对应的腿（a/b）和品种名。"""
    for leg in ("a", "b"):
        leg_venue, leg_symbol = mapping_leg(mapping, leg)
        if leg_venue == venue:
            return leg, leg_symbol
    return None


def _direction_is_venue_long(direction: str, leg: str) -> bool:
    """判断指定方向下，给定腿是否为多头方向。"""
    if direction == "long_leg_a_short_leg_b":
        return leg == "a"
    if direction == "long_leg_b_short_leg_a":
        return leg == "b"
    if direction == "long_mt5_short_hyperliquid":
        return leg == "b"
    return leg == "a"


def _float(value) -> float:
    """安全浮点转换。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
