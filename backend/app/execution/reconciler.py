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

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.mt5 import MT5Adapter
from app.adapters.venue import build_market_adapter, NATIVE_VENUES, nautilus_venues_from_mappings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import Alert, ExchangeCredential, Fill, HedgeGroup, HedgeGroupEvent, Order, Position, SymbolMapping, SystemLog, WorkerRun
from app.execution.gateway import LegOrderIntent, build_execution_gateway
from app.execution.hedge_pool import hedge_pool
from app.execution.pnl import actual_entry_spread_from_fills, realized_pnl_from_fills

logger = get_logger(__name__)

# 订单状态分类
PENDING_ORDER_STATUSES = {"accepted", "submitted", "pending", "open", "new"}
POSITION_EFFECT_STATUSES = {"filled", "partially_filled"}
FAILED_ORDER_STATUSES = {"failed", "rejected", "canceled", "expired", "unfilled", "not_found"}
UNRECONSTRUCTABLE_ORDER_STATUSES = {"not_ready", "not_supported"}
RECONCILE_GROUP_STATUSES = {"opening", "closing"}
MANAGED_POSITION_GROUP_STATUSES = {"opening", "open", "open_partial", "closing", "manual_intervention", "closed"}


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
    reconciled += reconcile_residual_positions(db)
    reconciled += reconcile_orphan_positions(db)
    # 同步完成后刷新内存对冲池
    hedge_pool.load_from_db(db)
    return reconciled


def sync_live_positions(db: Session) -> int:
    """从各交易平台同步 live 仓位到 Position 表。

    支持 Hyperliquid、MT5 以及 Nautilus 扩展 venue。
    """
    adapters = [HyperliquidAdapter(live=True), MT5Adapter(live=True)]
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = nautilus_venues_from_mappings(mappings)
    for (venue,) in db.query(ExchangeCredential.venue).filter(ExchangeCredential.enabled.is_(True)).all():
        if venue not in NATIVE_VENUES and venue not in venues:
            venues.append(venue)
    for venue in venues:
        adapters.append(build_market_adapter(venue, live=True))
    platforms = [adapter.platform for adapter in adapters]
    db.query(Position).filter(Position.platform.in_(platforms)).delete(synchronize_session=False)
    count = 0
    hyperliquid_dexes = _hyperliquid_position_dexes(db)
    for adapter in adapters:
        try:
            positions = adapter.get_positions(dexes=hyperliquid_dexes) if isinstance(adapter, HyperliquidAdapter) else adapter.get_positions()
        except Exception as exc:
            db.add(SystemLog(level="warning", category="execution_reconcile", message=f"{adapter.platform} 持仓同步失败", context=str(exc)))
            continue
        for item in positions:
            quantity = float(item.get("quantity", 0.0) or 0.0)
            if abs(quantity) <= 0:
                continue
            db.add(Position(
                platform=str(item.get("platform") or adapter.platform),
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ("long" if quantity > 0 else "short")),
                quantity=abs(quantity),
                entry_price=float(item.get("entry_price", 0.0) or 0.0),
                mark_price=float(item.get("mark_price", 0.0) or 0.0),
                unrealized_pnl=float(item.get("unrealized_pnl", 0.0) or 0.0),
                margin_used=float(item.get("margin_used", 0.0) or 0.0),
                liquidation_price=item.get("liquidation_price"),
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
    orders = db.query(Order).filter(Order.hedge_group_id == group.id).order_by(Order.id).all()
    changed = False
    changed = _recover_hyperliquid_orders_from_account(db, group, orders) or changed
    for order in orders:
        if order.status in PENDING_ORDER_STATUSES and order.external_order_id:
            changed = _refresh_order(db, group, order) or changed
    changed = _advance_group_state(db, group, orders) or changed
    changed = _escalate_stale_unreconstructable_group(db, group, orders) or changed
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
    positions = db.query(Position).filter(Position.platform.in_(list(NATIVE_VENUES))).all()
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
    """通过网关查询外部订单最新状态并更新本地记录。"""
    adapter = _adapter_for_order(order.platform, group)
    gateway = build_execution_gateway(adapter)
    snapshot = gateway.query_order(order.platform, order.external_order_id)
    status = str(snapshot.get("status") or order.status)
    changed = False
    if status in UNRECONSTRUCTABLE_ORDER_STATUSES:
        message = str(snapshot.get("message") or snapshot.get("error_message") or "外部订单状态不可重建")
        if message != order.error_message:
            order.error_message = message
            changed = True
        return changed
    if status and status != order.status:
        order.status = status
        changed = True
    message = str(snapshot.get("message") or snapshot.get("error_message") or "")
    if message and message != order.error_message:
        order.error_message = message
        changed = True
    filled_quantity = _float_value(snapshot, "filled_quantity", "quantity", "filled")
    average_price = _float_value(snapshot, "average_price", "price", "avg_price")
    fee = _float_value(snapshot, "fee", "commission")
    if filled_quantity > 0 and average_price > 0 and _order_fill_quantity(db, order.id) <= 0:
        db.add(Fill(order_id=order.id, platform=order.platform, symbol=order.symbol, side=order.side, quantity=filled_quantity, price=average_price, fee=fee))
        changed = True
    if hasattr(adapter, "get_trades") and _order_fill_quantity(db, order.id) <= 0:
        for trade in adapter.get_trades(order.external_order_id):
            quantity = float(trade.get("quantity", 0.0) or 0.0)
            price = float(trade.get("price", 0.0) or 0.0)
            if quantity <= 0 or price <= 0:
                continue
            db.add(Fill(order_id=order.id, platform=order.platform, symbol=order.symbol, side=order.side, quantity=quantity, price=price, fee=float(trade.get("fee", 0.0) or 0.0)))
            changed = True
    return changed


def _recover_hyperliquid_orders_from_account(db: Session, group: HedgeGroup, orders: list[Order]) -> bool:
    """从 Hyperliquid 账户历史订单中恢复未匹配的 pending 订单。"""
    if group.execution_mode != "live":
        return False
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    target_orders = [order for order in orders if order.platform == leg_a_venue and order.status in PENDING_ORDER_STATUSES]
    if not target_orders:
        return False
    if leg_a_venue == "hyperliquid":
        gateway = build_execution_gateway(HyperliquidAdapter(live=True))
    else:
        gateway = build_execution_gateway(build_market_adapter(leg_a_venue, live=True))
    query_account_orders = getattr(gateway, "query_account_orders", None)
    if not callable(query_account_orders):
        return False
    snapshots = query_account_orders(leg_a_venue)
    if not snapshots:
        return False
    changed = False
    by_external_id = _account_snapshots_by_external_id(snapshots)
    for order in target_orders:
        snapshot = by_external_id.get(str(order.external_order_id)) if order.external_order_id else None
        if snapshot is None and not order.external_order_id:
            matched = [item for item in snapshots if _account_snapshot_matches_order(db, group, order, item)]
            if len(matched) == 1:
                snapshot = matched[0]
        if snapshot is not None:
            changed = _apply_account_snapshot_to_order(db, order, snapshot) or changed
    return changed


def _account_snapshots_by_external_id(snapshots: list[dict]) -> dict[str, dict]:
    """按外部订单 ID 索引账户快照。"""
    by_external_id: dict[str, dict] = {}
    for snapshot in snapshots:
        for key in ("external_order_id", "oid", "cloid"):
            value = str(snapshot.get(key) or "")
            if value:
                by_external_id[value] = snapshot
        for alias in snapshot.get("external_order_ids", []) or []:
            value = str(alias or "")
            if value:
                by_external_id[value] = snapshot
    return by_external_id


def _apply_account_snapshot_to_order(db: Session, order: Order, snapshot: dict) -> bool:
    """将账户快照中的信息应用到本地订单记录。"""
    changed = False
    external_order_id = str(snapshot.get("external_order_id") or order.external_order_id or "")
    if external_order_id and external_order_id != order.external_order_id:
        order.external_order_id = external_order_id
        changed = True
    status = str(snapshot.get("status") or order.status)
    if status and status != order.status:
        order.status = status
        changed = True
    message = str(snapshot.get("message") or "")
    if message and message != order.error_message:
        order.error_message = message
        changed = True
    average_price = _float_value(snapshot, "average_price", "price", "avg_price")
    if average_price > 0 and order.price != average_price:
        order.price = average_price
        changed = True
    filled_quantity = _float_value(snapshot, "filled_quantity", "quantity", "filled")
    fee = _float_value(snapshot, "fee", "commission")
    if status in POSITION_EFFECT_STATUSES and filled_quantity > 0 and average_price > 0 and _order_fill_quantity(db, order.id) <= 0:
        db.add(Fill(order_id=order.id, platform=order.platform, symbol=order.symbol, side=order.side, quantity=filled_quantity, price=average_price, fee=fee))
        changed = True
    return changed


def _account_snapshot_matches_order(db: Session, group: HedgeGroup, order: Order, snapshot: dict) -> bool:
    """判断账户快照是否与本地订单匹配。"""
    if str(snapshot.get("side") or "") != order.side:
        return False
    if not _same_symbol(db, group, order, str(snapshot.get("symbol") or "")):
        return False
    quantity = _float_value(snapshot, "quantity", "filled_quantity")
    if quantity <= 0 or abs(quantity - order.quantity) > max(abs(order.quantity) * 0.000001, 0.00000001):
        return False
    timestamp_ms = int(snapshot.get("timestamp_ms") or 0)
    if timestamp_ms and order.created_at:
        snapshot_at = datetime.fromtimestamp(timestamp_ms / 1000)
        if snapshot_at < order.created_at:
            return False
    return True


def _same_symbol(db: Session, group: HedgeGroup, order: Order, external_symbol: str) -> bool:
    """检查外部品种名是否与本地订单/对冲组/品种映射匹配。"""
    symbols = {order.symbol, group.symbol}
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    if mapping:
        symbols.add(mapping.leg_a_venue_symbol)
    return external_symbol in symbols


def _advance_group_state(db: Session, group: HedgeGroup, orders: list[Order]) -> bool:
    """根据订单状态推进对冲组状态机。"""
    if not orders:
        return False
    platform_orders = _latest_platform_orders(orders)
    if group.status == "opening" and _complete_hyper_maker_with_mt5_taker(db, group, platform_orders):
        return True
    if group.status in {"opening", "closing"} and _complete_hyper_then_mt5_after_fill(db, group, platform_orders):
        return True
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
            canceled = _cancel_pending_orders(group, platform_orders.values())
            if _auto_compensate_single_leg(db, group, platform_orders.values(), "opening", canceled):
                return True
            group.status = "manual_intervention"
            detail = f"订单回查发现开仓单边成交，已尝试撤销未成交腿: {', '.join(canceled) or '无可撤订单'}"
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
            canceled = _cancel_pending_orders(group, platform_orders.values())
            if _auto_compensate_single_leg(db, group, platform_orders.values(), "closing", canceled):
                return True
            group.status = "manual_intervention"
            group.close_reason = f"平仓单边成交: {group.close_reason}; 已尝试撤销未成交腿: {', '.join(canceled) or '无可撤订单'}"
            db.add(Alert(level="critical", title="平仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} {group.close_reason}"))
            db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=group.close_reason))
            return True
        return any(pendings)
    return False


def _auto_compensate_single_leg(db, group, orders, phase, canceled) -> bool:
    """单腿成交时尝试自动反向冲销补偿。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    action = (mapping.single_leg_action if mapping else "manual_intervention") or "manual_intervention"
    if action not in {"auto_close", "reverse_filled_leg"}:
        return False
    filled_orders = [order for order in orders if _order_has_position_effect(db, order)]
    if len(filled_orders) != 1:
        return False
    filled_order = filled_orders[0]
    if _has_group_event(db, group.id, f"{phase}_single_leg_compensation"):
        return False
    compensation = _submit_compensation_order(db, group, filled_order, mapping)
    detail = (
        f"{'开仓' if phase == 'opening' else '平仓'}单腿成交，已尝试撤销未成交腿: {', '.join(canceled) or '无可撤订单'}; "
        f"按 single_leg_action={action} 反向冲销 {filled_order.platform}:{filled_order.external_order_id}; "
        f"补偿订单 {compensation.platform}:{compensation.external_order_id or compensation.id} 状态 {compensation.status}"
    )
    if _order_has_position_effect(db, compensation):
        group.fees += _orders_fee(db, [filled_order, compensation])
        if phase == "opening":
            group.status = "failed"
            group.close_reason = "开仓单腿成交后已自动反向冲销"
        else:
            group.status = "open"
            group.close_reason = f"平仓单腿成交后已自动反向冲销，恢复原对冲组: {group.close_reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=f"{phase}_single_leg_compensation", detail=detail))
        db.add(SystemLog(level="warning", category="execution_reconcile", message=f"单腿成交已自动补偿: {group.symbol} #{group.id}", context=detail))
        return True
    group.status = "manual_intervention"
    if phase == "closing":
        group.close_reason = f"平仓单腿成交且自动反向冲销未完成: {group.close_reason}; {detail}"
    else:
        group.close_reason = f"开仓单腿成交且自动反向冲销未完成: {detail}"
    db.add(Alert(level="critical", title="单腿自动补偿未完成", message=f"{group.symbol} 对冲组 #{group.id} {detail}"))
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=detail))
    return True


def _complete_hyper_maker_with_mt5_taker(db, group, platform_orders) -> bool:
    """Hyper maker 策略：maker 成交后补提交 MT5 taker 订单。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    if set(platform_orders) != {leg_a_venue}:
        return False
    if not mapping or mapping.execution_style != "hyper_maker_mt5_taker":
        return False
    hyper_order = platform_orders[leg_a_venue]
    if not _order_has_position_effect(db, hyper_order):
        return False
    if _has_group_event(db, group.id, "maker_fill_mt5_taker_submitted"):
        return False
    hyper_fill_quantity = _order_fill_quantity(db, hyper_order.id)
    hyper_target_quantity = float(group.leg_a_quantity or group.quantity or hyper_order.quantity)
    fill_ratio = min(max(hyper_fill_quantity / hyper_target_quantity, 0.0), 1.0) if hyper_target_quantity > 0 else 0.0
    mt5_quantity = float(group.leg_b_quantity or group.quantity or 0.0) * fill_ratio
    if mt5_quantity <= 0:
        return False
    mt5_side = "sell" if group.direction == "long_leg_a_short_leg_b" else "buy"
    mt5_order = _submit_order_for_group(db, group, platform="mt5", side=mt5_side, quantity=mt5_quantity, order_type="market", venue_symbol=mapping.mt5_symbol)
    detail = f"Hyperliquid maker 后续成交 {hyper_fill_quantity}/{hyper_target_quantity}，已按比例提交 MT5 taker: {mt5_order.status}"
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="maker_fill_mt5_taker_submitted", detail=detail))
    if _order_has_position_effect(db, mt5_order):
        group.status = "open"
        group.opened_at = group.opened_at or utc_now()
        group.fees += _orders_fee(db, [hyper_order, mt5_order])
        actual_entry_spread = actual_entry_spread_from_fills(db, group)
        if actual_entry_spread is not None:
            group.entry_spread = actual_entry_spread
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="opened_reconciled", detail="Hyper maker 后续成交后 MT5 taker 补单完成"))
    elif mt5_order.status in PENDING_ORDER_STATUSES:
        group.status = "opening"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="orders_pending", detail=detail))
    else:
        group.status = "manual_intervention"
        db.add(Alert(level="critical", title="MT5 taker 补单失败", message=f"{group.symbol} 对冲组 #{group.id} {detail}; {mt5_order.error_message or ''}"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=detail))
    return True


def _complete_hyper_then_mt5_after_fill(db, group, platform_orders) -> bool:
    """串行策略：Hyperliquid 成交后按比例补提交 MT5 订单。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    if set(platform_orders) != {leg_a_venue}:
        return False
    if not mapping or mapping.execution_style == "hyper_maker_mt5_taker":
        return False
    hyper_order = platform_orders[leg_a_venue]
    if not _order_has_position_effect(db, hyper_order):
        return False
    event_type = f"{group.status}_hyper_fill_mt5_submitted"
    if _has_group_event(db, group.id, event_type):
        return False
    hyper_fill_quantity = _order_fill_quantity(db, hyper_order.id)
    hyper_target_quantity = float(group.leg_a_quantity or group.quantity or hyper_order.quantity)
    fill_ratio = min(max(hyper_fill_quantity / hyper_target_quantity, 0.0), 1.0) if hyper_target_quantity > 0 else 0.0
    mt5_quantity = float(group.leg_b_quantity or group.quantity or 0.0) * fill_ratio
    if mt5_quantity <= 0:
        return False
    if group.status == "opening":
        mt5_side = "sell" if group.direction == "long_leg_a_short_leg_b" else "buy"
        order_type = mapping.mt5_open_order_type
        reduce_only = False
        success_status = "open"
        success_event = "opened_reconciled"
        pending_event = "orders_pending"
        failure_title = "MT5 开仓补单失败"
        success_detail = "Hyperliquid 成交后 MT5 开仓补单完成"
    else:
        mt5_side = "buy" if group.direction == "long_leg_a_short_leg_b" else "sell"
        order_type = mapping.mt5_close_order_type
        reduce_only = True
        success_status = "closed"
        success_event = "closed_reconciled"
        pending_event = "close_pending"
        failure_title = "MT5 平仓补单失败"
        success_detail = "Hyperliquid 平仓成交后 MT5 平仓补单完成"
    mt5_order = _submit_order_for_group(db, group, platform="mt5", side=mt5_side, quantity=mt5_quantity, order_type=order_type, venue_symbol=mapping.mt5_symbol, reduce_only=reduce_only)
    detail = f"Hyperliquid 后续成交 {hyper_fill_quantity}/{hyper_target_quantity}，已按比例提交 MT5 {'平仓' if reduce_only else '开仓'}补单: {mt5_order.status}"
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=event_type, detail=detail))
    if _order_has_position_effect(db, mt5_order):
        group.status = success_status
        group.fees += _orders_fee(db, [hyper_order, mt5_order])
        if success_status == "open":
            group.opened_at = group.opened_at or utc_now()
            actual_entry_spread = actual_entry_spread_from_fills(db, group)
            if actual_entry_spread is not None:
                group.entry_spread = actual_entry_spread
        else:
            group.closed_at = group.closed_at or utc_now()
            group.realized_pnl = realized_pnl_from_fills(db, group)
            if group.realized_pnl is None:
                group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
            group.unrealized_pnl = 0.0
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=success_event, detail=success_detail))
    elif mt5_order.status in PENDING_ORDER_STATUSES:
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=pending_event, detail=detail))
    else:
        group.status = "manual_intervention"
        if reduce_only:
            group.close_reason = f"平仓 MT5 补单失败: {group.close_reason}; {mt5_order.error_message or ''}"
        db.add(Alert(level="critical", title=failure_title, message=f"{group.symbol} 对冲组 #{group.id} {detail}; {mt5_order.error_message or ''}"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=detail))
    return True


def _submit_order_for_group(db, group, *, platform, side, quantity, order_type, venue_symbol, reduce_only=False) -> Order:
    """提交订单到执行网关并记录到数据库。"""
    order = Order(hedge_group_id=group.id, platform=platform, symbol=group.symbol, side=side, quantity=quantity, order_type=order_type, reduce_only=reduce_only, status="new")
    db.add(order)
    db.flush()
    adapter = _adapter_for_order(platform, group)
    gateway = build_execution_gateway(adapter)
    result = gateway.submit_order(LegOrderIntent(platform=platform, symbol=group.symbol, side=side, quantity=quantity, venue_symbol=venue_symbol, order_type=order_type, reduce_only=reduce_only, hedge_group_id=group.id))
    order.status = result.adapter_result.status
    order.external_order_id = result.adapter_result.external_order_id
    order.price = result.adapter_result.average_price or None
    order.error_message = result.adapter_result.error_message
    for fill_event in result.fill_events:
        db.add(Fill(order_id=order.id, platform=fill_event.platform, symbol=fill_event.symbol, side=fill_event.side, quantity=fill_event.quantity, price=fill_event.price, fee=fill_event.fee))
    return order


def _submit_compensation_order(db, group, filled_order, mapping) -> Order:
    """提交反向冲销补偿订单。"""
    side = "sell" if filled_order.side == "buy" else "buy"
    quantity = _order_fill_quantity(db, filled_order.id) or filled_order.quantity
    venue_symbol = _venue_symbol_for_order(group, filled_order, mapping)
    return _submit_order_for_group(db, group, platform=filled_order.platform, side=side, quantity=quantity, order_type="market", venue_symbol=venue_symbol, reduce_only=True)


def _venue_symbol_for_order(group, order, mapping) -> str:
    """根据订单平台和品种映射确定外部品种名。"""
    if not mapping:
        return order.symbol
    if order.platform == mapping.leg_b_venue or order.platform == "mt5":
        return mapping.mt5_symbol
    if order.platform == mapping.leg_a_venue or order.platform == "hyperliquid":
        return mapping.leg_a_venue_symbol
    return group.symbol


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
    canceled = _cancel_pending_orders(group, stale_orders)
    detail = "; ".join(f"{order.platform}:{order.external_order_id}:{order.error_message}" for order in stale_orders)
    suffix = f"外部订单状态超过 {stale_seconds}s 不可重建: {detail}; 已尝试撤销: {', '.join(canceled) or '无可撤订单'}"
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

def _adapter_for_order(platform: str, group: HedgeGroup):
    """根据平台和对冲组执行模式构建适配器。"""
    live = group.execution_mode == "live"
    simulated = group.execution_mode == "paper"
    if platform == "mt5":
        return MT5Adapter(live=live, demo=simulated)
    adapter = HyperliquidAdapter(live=live)
    setattr(adapter, "simulated", simulated)
    return adapter


def _cancel_pending_orders(group: HedgeGroup, orders) -> list[str]:
    """尝试撤销 pending 状态的订单。"""
    canceled: list[str] = []
    for order in orders:
        if order.status not in PENDING_ORDER_STATUSES or not order.external_order_id:
            continue
        adapter = _adapter_for_order(order.platform, group)
        gateway = build_execution_gateway(adapter)
        if gateway.cancel_order(order.platform, order.external_order_id):
            order.status = "canceled"
            canceled.append(f"{order.platform}:{order.external_order_id}")
        else:
            order.error_message = "自动撤销未成交腿失败"
    return canceled


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
