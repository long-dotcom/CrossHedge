"""
执行对账模块
==============

定期扫描处于 ``opening`` / ``closing`` 中间状态的对冲组，
通过查询外部订单状态来推进对冲组状态机：

- ``sync_live_positions`` — 从各平台同步 live 仓位到 Position 表
- ``reconcile_hedge_group`` — 对单个对冲组执行订单回查和状态推进
- ``reconcile_residual_positions`` — 检测平仓后的残余仓位
- ``reconcile_orphan_positions`` — 检测未归属的孤儿仓位

使用方式::

    from app.core.db_session import db_session
    from app.execution.reconciler import run_execution_reconcile

    with db_session() as db:
        count = run_execution_reconcile(db)
"""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import Alert, ExchangeCredential, ExecutionIntent, Fill, HedgeGroup, HedgeGroupEvent, Order, Position, SymbolMapping, SystemLog, WorkerRun
from app.execution.event_projection import project_legacy_order, project_legacy_orders, project_unmirrored_legacy_orders
from app.execution.hedge_pool import hedge_pool
from app.execution.pnl import actual_entry_spread_from_fills, realized_pnl_from_fills
from app.venues.domain.models import Position as VenuePosition, PositionSide
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)

# 订单状态分类
PENDING_ORDER_STATUSES = {
    "initialized", "released", "emulated",
    "accepted", "submitted", "pending", "open", "new",
}
POSITION_EFFECT_STATUSES = {"filled", "partially_filled"}
FAILED_ORDER_STATUSES = {"failed", "rejected", "canceled", "expired", "unfilled", "not_found"}
UNRECONSTRUCTABLE_ORDER_STATUSES = {"not_ready", "not_supported"}
RECONCILE_GROUP_STATUSES = {"opening", "closing"}
MANAGED_POSITION_GROUP_STATUSES = {"opening", "open", "open_partial", "closing", "manual_intervention", "closed"}
SUPPORTED_VENUES = {"hyperliquid", "mt5", "binance"}


def run_execution_reconcile(db: Session) -> int:
    """执行对账入口 —— 同步仓位、回查中间状态对冲组、检测异常仓位。

    使用 ``run_worker`` 模板包装，自动记录 WorkerRun 并执行 prune。

    参数:
        db: 数据库会话。

    返回:
        发生变化的对冲组 / 仓位数量。
    """
    return run_worker(
        db,
        "execution_reconciler",
        _reconcile_impl,
        prune_models=[WorkerRun],
    )


def _reconcile_impl(db: Session) -> int:
    """对账内部实现。"""
    reconciled = 0
    sync_live_positions(db)
    groups = db.query(HedgeGroup).filter(HedgeGroup.status.in_(RECONCILE_GROUP_STATUSES)).all()
    for group in groups:
        changed = reconcile_hedge_group(db, group)
        reconciled += 1 if changed else 0
    reconciled += reconcile_unresolved_orders(db, exclude_group_ids={group.id for group in groups})
    reconciled += reconcile_residual_positions(db)
    reconciled += reconcile_orphan_positions(db)
    # 分批回填历史订单；幂等事件 ID 保证重启或重复扫描不会重复写入。
    project_unmirrored_legacy_orders(db)
    # 同步完成后刷新内存对冲池
    hedge_pool.load_from_db(db)
    return reconciled


def reconcile_unresolved_orders(db: Session, *, exclude_group_ids: set[int] | None = None) -> int:
    """同步已离开活动状态但仍未确认终态的外部订单。

    原生私有事件会持续推进订单。本函数负责把断线期间迟到的 ACK、
    Fill、Cancel 等结果写回业务数据库，避免对冲组提前进入 failed 后丢失成交。
    """
    excluded = exclude_group_ids or set()
    orders = (
        db.query(Order)
        .filter(
            Order.status.in_(PENDING_ORDER_STATUSES | {"partially_filled"}),
            Order.external_order_id != "",
            Order.hedge_group_id.isnot(None),
        )
        .order_by(Order.id)
        .all()
    )
    changed_count = 0
    for order in orders:
        if order.hedge_group_id in excluded:
            continue
        group = db.query(HedgeGroup).filter(HedgeGroup.id == order.hedge_group_id).first()
        if group is None:
            continue
        had_position_effect = _order_has_position_effect(db, order)
        changed = _refresh_order(db, group, order)
        escalated = _escalate_detached_unresolved_order(db, group, order)
        project_legacy_order(db, order)
        if not changed and not escalated:
            continue
        changed_count += 1
        if escalated:
            continue
        if had_position_effect or not _order_has_position_effect(db, order):
            continue
        detail = (
            f"对冲组已处于 {group.status}，但外部订单随后确认成交: "
            f"{order.platform}:{order.external_order_id}:{order.status}"
        )
        group.status = "manual_intervention"
        if not _has_group_event(db, group.id, "late_external_fill"):
            db.add(Alert(level="critical", title="已结束对冲组出现迟到成交", message=f"{group.symbol} 对冲组 #{group.id} {detail}"))
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="late_external_fill", detail=detail))
            db.add(SystemLog(level="warning", category="execution_reconcile", message=f"迟到成交: {group.symbol} #{group.id}", context=detail))
    return changed_count


def _escalate_detached_unresolved_order(db: Session, group: HedgeGroup, order: Order) -> bool:
    """对超过阈值仍无法由原生对账重建的离线订单升级人工介入。"""
    if not order.error_message or _order_age_seconds(order) < max(int(get_settings().execution.reconcile_pending_stale_seconds), 1):
        return False
    if _has_group_event(db, group.id, "detached_order_reconcile_required"):
        return False
    detail = (
        f"外部订单长期无法重建: {order.platform}:{order.external_order_id}:{order.error_message}; "
        "旧订单仅升级人工恢复，禁止对账器绕过 Coordinator 自动撤单"
    )
    group.status = "manual_intervention"
    db.add(Alert(level="critical", title="历史外部订单无法确认", message=f"{group.symbol} 对冲组 #{group.id} {detail}"))
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="detached_order_reconcile_required", detail=detail))
    db.add(SystemLog(level="warning", category="execution_reconcile", message=f"历史外部订单无法确认: {group.symbol} #{group.id}", context=detail))
    return True


def sync_live_positions(db: Session) -> int:
    """从各交易平台同步 live 仓位到 Position 表。

    支持 Hyperliquid、MT5 和 Binance 原生连接器。
    """
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = {
        str(value or "").strip().lower()
        for mapping in mappings
        for value in (mapping.leg_a_venue, mapping.leg_b_venue)
    }
    venues.update({"hyperliquid", "mt5"})
    for (venue,) in db.query(ExchangeCredential.venue).filter(ExchangeCredential.enabled.is_(True)).all():
        venues.add(str(venue or "").strip().lower())
    count = 0
    for venue in sorted(venues & SUPPORTED_VENUES):
        try:
            positions = native_venue_manager.connector_for(venue, "live").get_positions()
        except Exception as exc:
            db.add(SystemLog(level="warning", category="execution_reconcile", message=f"{venue} 持仓同步失败", context=str(exc)))
            continue
        # 只有拿到该平台完整快照后才替换旧数据，断线不能把最后已知持仓清空。
        db.query(Position).filter(Position.platform == venue).delete(synchronize_session=False)
        for item in positions:
            quantity = float(item.quantity)
            if abs(quantity) <= 0:
                continue
            db.add(Position(
                platform=venue,
                symbol=item.symbol,
                side=_venue_position_side(item),
                quantity=abs(quantity),
                entry_price=float(item.entry_price),
                mark_price=float(item.mark_price),
                unrealized_pnl=float(item.unrealized_pnl),
                margin_used=float(item.margin_used),
                liquidation_price=float(item.liquidation_price) if item.liquidation_price is not None else None,
            ))
            count += 1
    return count


def _hyperliquid_position_dexes(db: Session) -> list[str]:
    """从品种映射中提取 Hyperliquid DEX 列表。"""
    dexes: list[str] = []
    rows = db.query(SymbolMapping.leg_a_venue_symbol).filter(SymbolMapping.enabled.is_(True)).all()
    for (symbol,) in rows:
        value = str(symbol or "")
        if ":" not in value:
            continue
        dex = value.split(":", 1)[0].strip()
        if dex and dex not in dexes:
            dexes.append(dex)
    return dexes


def reconcile_hedge_group(db: Session, group: HedgeGroup) -> bool:
    """对单个对冲组执行订单回查和状态推进。

    流程：
    1. 从 Hyperliquid 账户回查未匹配订单
    2. 刷新 pending 状态的外部订单
    3. 推进对冲组状态机
    4. 升级过期不可重建的订单
    """
    # 新模型完全由原生事件投影和 Outbox 状态机推进；旧对账器不得再次提交、
    # 撤销或补偿这些订单，否则会绕过稳定 ClientOrderId 和恢复协议。
    if db.query(ExecutionIntent.id).filter(ExecutionIntent.hedge_group_id == group.id).first() is not None:
        return False
    orders = db.query(Order).filter(Order.hedge_group_id == group.id).order_by(Order.id).all()
    changed = False
    changed = _recover_hyperliquid_orders_from_account(db, group, orders) or changed
    for order in orders:
        if (order.status in PENDING_ORDER_STATUSES or (order.post_only and order.status == "partially_filled")) and order.external_order_id:
            changed = _refresh_order(db, group, order) or changed
    changed = _advance_group_state(db, group, orders) or changed
    changed = _escalate_stale_unreconstructable_group(db, group, orders) or changed
    project_legacy_orders(db, orders)
    return changed


def reconcile_residual_positions(db: Session) -> int:
    """检测已关闭 live 对冲组的残余仓位。"""
    changed = 0
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live", HedgeGroup.status == "closed").all()
    for group in groups:
        residual = _residual_positions_for_group(db, group)
        if not residual:
            continue
        if _has_group_event(db, group.id, "residual_position"):
            continue
        group.status = "manual_intervention"
        detail = "; ".join(f"{row.platform}:{row.symbol}:{row.side}:{row.quantity}" for row in residual)
        group.close_reason = f"平仓后发现残余仓位: {detail}"
        db.add(Alert(level="critical", title="平仓后残余仓位", message=f"{group.symbol} 对冲组 #{group.id} 需要人工核对: {detail}"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="residual_position", detail=detail))
        db.add(SystemLog(level="warning", category="execution_reconcile", message=f"平仓后残余仓位: {group.symbol} #{group.id}", context=detail))
        changed += 1
    return changed


def reconcile_orphan_positions(db: Session) -> int:
    """检测未归属任何 live 对冲组的外部仓位。"""
    changed = 0
    positions = db.query(Position).filter(Position.platform.in_(list(SUPPORTED_VENUES))).all()
    for position in positions:
        if abs(position.quantity) <= 0:
            continue
        if _position_has_live_group(db, position):
            continue
        detail = f"{position.platform}:{position.symbol}:{position.side}:{position.quantity}"
        message = f"外部账户存在未归属 live 对冲组的仓位: {detail}"
        if _has_open_alert(db, "外部孤儿仓位", message):
            continue
        db.add(Alert(level="critical", title="外部孤儿仓位", message=message))
        db.add(SystemLog(level="warning", category="execution_reconcile", message="外部孤儿仓位", context=detail))
        changed += 1
    return changed


def _refresh_order(db: Session, group: HedgeGroup, order: Order) -> bool:
    """通过原生连接器查询外部订单最新状态并更新本地记录。"""
    connector = native_venue_manager.connector_for(order.platform, group.execution_mode)
    symbol = _venue_symbol_for_order(db, group, order)
    try:
        snapshot = connector.get_order(symbol, venue_order_id=order.external_order_id)
    except Exception as exc:
        message = f"外部订单状态暂不可重建: {exc}"
        if message != order.error_message:
            order.error_message = message
            return True
        return False
    status = snapshot.status.value.lower()
    changed = False
    if status and status != order.status:
        order.status = status
        changed = True
    if order.error_message:
        order.error_message = ""
        changed = True
    filled_quantity = float(snapshot.filled_quantity)
    average_price = float(snapshot.average_price or snapshot.price or 0)
    fee = float(snapshot.commission)
    recorded_quantity = _order_fill_quantity(db, order.id)
    fill_delta = max(filled_quantity - recorded_quantity, 0.0)
    if fill_delta > 0 and average_price > 0:
        fee_delta = fee * (fill_delta / filled_quantity) if filled_quantity > 0 else 0.0
        db.add(Fill(order_id=order.id, platform=order.platform, symbol=order.symbol, side=order.side, quantity=fill_delta, price=average_price, fee=fee_delta))
        changed = True
    if filled_quantity <= 0 and recorded_quantity <= 0:
        for trade in connector.get_fills(symbol, venue_order_id=order.external_order_id):
            quantity = float(trade.quantity)
            price = float(trade.price)
            if quantity <= 0 or price <= 0:
                continue
            db.add(Fill(order_id=order.id, platform=order.platform, symbol=order.symbol, side=order.side, quantity=quantity, price=price, fee=float(trade.commission)))
            changed = True
    return changed


def _recover_hyperliquid_orders_from_account(db: Session, group: HedgeGroup, orders: list[Order]) -> bool:
    """从原生账户活动订单中恢复未匹配的 pending 订单。"""
    if group.execution_mode != "live":
        return False
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    target_orders = [order for order in orders if order.platform == leg_a_venue and order.status in PENDING_ORDER_STATUSES]
    if not target_orders:
        return False
    connector = native_venue_manager.connector_for(leg_a_venue, "live")
    snapshots = connector.get_open_orders()
    if not snapshots:
        return False
    changed = False
    by_external_id = {snapshot.venue_order_id: snapshot for snapshot in snapshots if snapshot.venue_order_id}
    for order in target_orders:
        snapshot = by_external_id.get(str(order.external_order_id)) if order.external_order_id else None
        if snapshot is not None:
            external_order_id = snapshot.venue_order_id
            status = snapshot.status.value.lower()
            if external_order_id and external_order_id != order.external_order_id:
                order.external_order_id = external_order_id
                changed = True
            if status != order.status:
                order.status = status
                changed = True
    return changed


def _advance_group_state(db: Session, group: HedgeGroup, orders: list[Order]) -> bool:
    """根据订单状态推进对冲组状态机。"""
    if not orders:
        return False
    platform_orders = _latest_platform_orders(orders)
    if len(platform_orders) < 2:
        return False
    effects = [_order_has_position_effect(db, order) for order in platform_orders.values()]
    failures = [_order_is_terminal_failure(order) for order in platform_orders.values()]
    pendings = [order.status in PENDING_ORDER_STATUSES for order in platform_orders.values()]

    if group.status == "opening":
        if all(effects):
            group.status = "open"
            group.opened_at = group.opened_at or utc_now()
            group.fees += _orders_fee(db, platform_orders.values())
            actual_entry_spread = actual_entry_spread_from_fills(db, group)
            if actual_entry_spread is not None:
                group.entry_spread = actual_entry_spread
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="opened_reconciled", detail="订单回查确认双边开仓成交"))
            db.add(SystemLog(level="info", category="execution_reconcile", message=f"开仓回查完成: {group.symbol} #{group.id}"))
            logger.info("对账开仓完成: symbol={} group=#{}", group.symbol, group.id)
            return True
        if any(effects) and (any(failures) or any(pendings)):
            group.status = "manual_intervention"
            detail = "旧订单回查发现开仓单边成交；已禁止旧对账器自动撤单/补偿，请通过恢复 Intent 处理"
            db.add(Alert(level="critical", title="开仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} {detail}"))
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=detail))
            return True
        if all(failures):
            group.status = "failed"
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="failed_reconciled", detail="订单回查确认双边开仓失败"))
            return True
        return any(pendings)

    if group.status == "closing":
        if all(effects):
            group.status = "closed"
            group.closed_at = group.closed_at or utc_now()
            group.fees += _orders_fee(db, platform_orders.values())
            group.realized_pnl = realized_pnl_from_fills(db, group)
            if group.realized_pnl is None:
                group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
            group.unrealized_pnl = 0.0
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="closed_reconciled", detail="订单回查确认双边平仓成交"))
            db.add(SystemLog(level="info", category="execution_reconcile", message=f"平仓回查完成: {group.symbol} #{group.id}"))
            logger.info("对账平仓完成: symbol={} group=#{}", group.symbol, group.id)
            return True
        if any(effects) and (any(failures) or any(pendings)):
            group.status = "manual_intervention"
            group.close_reason = f"平仓单边成交: {group.close_reason}; 旧对账器禁止自动撤单/补偿，请通过恢复 Intent 处理"
            db.add(Alert(level="critical", title="平仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} {group.close_reason}"))
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=group.close_reason))
            return True
        return any(pendings)
    return False




def _escalate_stale_unreconstructable_group(db, group, orders) -> bool:
    """升级过期且外部订单状态不可重建的对冲组为人工介入。"""
    if group.status not in RECONCILE_GROUP_STATUSES:
        return False
    settings = get_settings()
    stale_seconds = max(int(settings.execution.reconcile_pending_stale_seconds), 1)
    stale_orders = [
        order for order in orders
        if order.status in PENDING_ORDER_STATUSES
        and order.external_order_id
        and order.error_message
        and _order_age_seconds(order) >= stale_seconds
    ]
    if not stale_orders:
        return False
    detail = "; ".join(f"{order.platform}:{order.external_order_id}:{order.error_message}" for order in stale_orders)
    suffix = f"外部订单状态超过 {stale_seconds}s 不可重建: {detail}; 禁止旧对账器自动撤单，请通过恢复 Intent 处理"
    group.status = "manual_intervention"
    if group.close_reason:
        group.close_reason = f"{group.close_reason}; {suffix}"
    else:
        group.close_reason = suffix
    db.add(Alert(level="critical", title="外部订单状态不可重建", message=f"{group.symbol} 对冲组 #{group.id} {suffix}"))
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="external_reconcile_required", detail=suffix))
    db.add(SystemLog(level="warning", category="execution_reconcile", message=f"外部订单状态不可重建: {group.symbol} #{group.id}", context=suffix))
    return True


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _venue_symbol_for_order(db: Session, group: HedgeGroup, order: Order) -> str:
    """把业务组合品种转换为交易所实际品种。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if mapping is None:
        return order.symbol
    if str(mapping.leg_a_venue or "").lower() == order.platform:
        return str(mapping.leg_a_venue_symbol or order.symbol)
    if str(mapping.leg_b_venue or "").lower() == order.platform:
        return str(mapping.mt5_symbol or order.symbol)
    return order.symbol


def _venue_position_side(item: VenuePosition) -> str:
    """把统一仓位方向转换为旧数据库展示字段。"""
    if item.position_side == PositionSide.LONG:
        return "long"
    if item.position_side == PositionSide.SHORT:
        return "short"
    signed = item.raw.get("positionAmt", item.raw.get("szi", item.quantity))
    try:
        return "short" if float(signed) < 0 else "long"
    except (TypeError, ValueError):
        return "long"


def _residual_positions_for_group(db: Session, group: HedgeGroup) -> list[Position]:
    """查找已关闭对冲组的残余仓位。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    symbols = {leg_a_venue: {group.symbol}, leg_b_venue: {group.symbol}}
    if mapping:
        symbols[leg_a_venue].add(mapping.leg_a_venue_symbol)
        symbols[leg_b_venue].add(mapping.mt5_symbol)
    residual: list[Position] = []
    for platform, names in symbols.items():
        rows = db.query(Position).filter(Position.platform == platform, Position.symbol.in_(names)).all()
        residual.extend(row for row in rows if abs(row.quantity) > 0)
    return residual


def _position_has_live_group(db: Session, position: Position) -> bool:
    """检查仓位是否归属于某个活跃的 live 对冲组。"""
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live", HedgeGroup.status.in_(MANAGED_POSITION_GROUP_STATUSES)).all()
    return any(_position_matches_group(db, position, group) for group in groups)


def _position_matches_group(db: Session, position: Position, group: HedgeGroup) -> bool:
    """判断仓位是否与对冲组匹配。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    if position.platform not in {leg_a_venue, leg_b_venue}:
        return False
    symbols = {leg_a_venue: {group.symbol}, leg_b_venue: {group.symbol}}
    if mapping:
        if mapping.leg_a_venue_symbol:
            symbols[leg_a_venue].add(mapping.leg_a_venue_symbol)
        if mapping.mt5_symbol:
            symbols[leg_b_venue].add(mapping.mt5_symbol)
    if position.symbol not in symbols.get(position.platform, set()):
        return False
    if _position_side(position.side) != _expected_position_side(group.direction, position.platform):
        return False
    if group.status == "closed":
        return True
    expected_quantity = _expected_position_quantity(group, position.platform)
    if expected_quantity <= 0:
        return False
    tolerance = max(expected_quantity * 0.000001, 0.00000001)
    return abs(abs(position.quantity) - expected_quantity) <= tolerance


def _expected_position_side(direction: str, platform: str) -> str:
    """根据方向推断指定平台的预期仓位方向。"""
    if direction == "long_leg_a_short_leg_b":
        if platform == "hyperliquid":
            return "long"
        return "short"
    return "short" if platform == "hyperliquid" else "long"


def _expected_position_quantity(group: HedgeGroup, platform: str) -> float:
    """根据方向推断指定平台的预期仓位数量。"""
    if platform == "hyperliquid":
        value = group.leg_a_quantity
    else:
        value = group.leg_b_quantity
    # 优先使用腿数量，若为空则回退到组合数量
    result = value if value is not None else group.quantity
    return float(result) if result is not None else 0.0


def _position_side(side: str) -> str:
    """标准化仓位方向字符串。"""
    value = str(side or "").strip().lower()
    if value in {"buy", "long"}:
        return "long"
    if value in {"sell", "short"}:
        return "short"
    return value


def _has_open_alert(db: Session, title: str, message: str) -> bool:
    """检查是否已存在相同的未确认告警。"""
    return db.query(Alert).filter(Alert.title == title, Alert.message == message, Alert.acknowledged == False).first() is not None  # noqa: E712


def _has_group_event(db: Session, group_id: int, event_type: str) -> bool:
    """检查对冲组是否已存在指定类型的事件。"""
    return db.query(HedgeGroupEvent).filter(HedgeGroupEvent.hedge_group_id == group_id, HedgeGroupEvent.event_type == event_type).first() is not None


def _latest_platform_orders(orders: list[Order]) -> dict[str, Order]:
    """按平台取最新订单（以列表顺序，后者覆盖前者）。"""
    latest: dict[str, Order] = {}
    for order in orders:
        latest[order.platform] = order
    return latest


def _order_has_position_effect(db: Session, order: Order) -> bool:
    """判断订单是否已产生仓位变化（已成交且有 Fill）。"""
    return order.status in POSITION_EFFECT_STATUSES and _order_fill_quantity(db, order.id) > 0


def _order_is_terminal_failure(order: Order) -> bool:
    """判断订单是否处于终态失败。"""
    return order.status in FAILED_ORDER_STATUSES


def _order_age_seconds(order: Order) -> float:
    """计算订单从创建到现在的时长（秒）。"""
    created_at = order.created_at
    if not created_at:
        return 0.0
    return max((utc_now() - created_at).total_seconds(), 0.0)


def _order_fill_quantity(db: Session, order_id: int) -> float:
    """计算订单的累计成交量。"""
    return sum(row.quantity for row in db.query(Fill).filter(Fill.order_id == order_id).all())


def _orders_fee(db: Session, orders) -> float:
    """计算订单列表的累计手续费。"""
    order_ids = [order.id for order in orders]
    if not order_ids:
        return 0.0
    return sum(row.fee for row in db.query(Fill).filter(Fill.order_id.in_(order_ids)).all())


def _float_value(snapshot: dict, *keys: str) -> float:
    """从字典中按优先级尝试多个键，返回第一个有效浮点值。"""
    for key in keys:
        value = snapshot.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0
