"""可靠执行 Outbox Worker。

Worker 先在短事务中领取命令并持久化稳定 ``client_order_id``，再调用 venue。
首次领取允许提交；进程崩溃留下的 ``PROCESSING`` 命令只允许查询恢复，绝不盲目
重发，从而避免“交易所已收到、数据库尚未写回”窗口导致重复下单。
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ExecutionEvent, ExecutionIntent, ExecutionLeg, ExecutionOutbox, Fill, HedgeGroup, HedgeGroupEvent, Order, SystemLog, VenueOrder
from app.db.session import SessionLocal
from app.venues.domain.models import OrderRequest, OrderStatus, OrderType, PositionSide, Side, TimeInForce
from app.venues.manager import native_venue_manager


AdapterFactory = Callable[[str, str], Any]
logger = get_logger(__name__)

NON_TERMINAL_ORDER_STATUSES = {
    "INITIALIZED", "NEW", "SUBMITTED", "ACCEPTED", "PENDING", "OPEN",
    "PARTIALLY_FILLED", "PENDING_UPDATE", "PENDING_CANCEL", "NOT_READY", "UNKNOWN",
}
SUCCESS_ORDER_STATUSES = {"FILLED"}
FAILURE_ORDER_STATUSES = {"FAILED", "REJECTED", "DENIED", "CANCELED", "CANCELLED", "EXPIRED", "UNFILLED"}


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    outbox_id: int
    recovery_only: bool


@dataclass(frozen=True, slots=True)
class DispatchCommand:
    """可安全跨线程传递的不可变单腿提交命令。"""

    leg_id: int
    intent_id: int
    venue_order_id: int
    venue: str
    execution_mode: str
    symbol: str
    venue_symbol: str
    side: str
    quantity: float
    order_type: str
    price: float | None
    post_only: bool
    venue_reduce_only: bool
    position_side: str
    action: str
    hedge_group_id: int | None
    client_order_id: str


def run_execution_outbox_once(
    *,
    session_factory: sessionmaker = SessionLocal,
    adapter_factory: AdapterFactory | None = None,
    limit: int = 20,
    processing_timeout_seconds: int = 30,
) -> int:
    """领取并处理一批 Outbox 命令，返回领取数量。"""
    factory = adapter_factory or _default_adapter_factory
    # 私有 WS 是订单确认主路径；REST 查单只允许启动或断线重连后的单次补偿。
    from app.execution.venue_events import consume_reconciliation_request, project_venue_events_once

    project_venue_events_once(session_factory=session_factory)
    advance_execution_timers_once(session_factory=session_factory, adapter_factory=factory)
    if consume_reconciliation_request():
        reconcile_execution_orders_once(
            session_factory=session_factory,
            adapter_factory=factory,
            limit=max(int(limit) * 4, 20),
        )
        from app.execution.probe_runs import reconcile_probe_runs_once

        reconcile_probe_runs_once(session_factory=session_factory, adapter_factory=factory)
    claims = _claim_outbox(
        session_factory,
        limit=max(int(limit), 1),
        processing_timeout_seconds=max(int(processing_timeout_seconds), 1),
    )
    for claim in claims:
        _process_claim(session_factory, factory, claim)
    return len(claims)


def advance_execution_timers_once(
    *,
    session_factory: sessionmaker = SessionLocal,
    adapter_factory: AdapterFactory | None = None,
) -> int:
    """仅按本地持久化状态推进 Maker TTL，不向交易所发起查单。"""
    factory = adapter_factory or _default_adapter_factory
    advanced = 0
    with session_factory() as db:
        intents = db.query(ExecutionIntent).filter(
            ExecutionIntent.status == "RUNNING",
            ExecutionIntent.execution_style == "maker_then_market",
        ).all()
        for intent in intents:
            outbox = (
                db.query(ExecutionOutbox)
                .filter(ExecutionOutbox.intent_id == intent.id)
                .order_by(ExecutionOutbox.id.desc())
                .first()
            )
            legs = db.query(ExecutionLeg).filter(
                ExecutionLeg.intent_id == intent.id
            ).order_by(ExecutionLeg.id).all()
            if outbox is None or not legs:
                continue
            before = (intent.status, outbox.status, len(legs))
            _finish_command(db, outbox, intent, legs, adapter_factory=factory)
            db.flush()
            after = (intent.status, outbox.status, db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).count())
            advanced += int(after != before)
        db.commit()
    return advanced


def reconcile_execution_orders_once(
    *,
    session_factory: sessionmaker = SessionLocal,
    adapter_factory: AdapterFactory | None = None,
    limit: int = 100,
) -> int:
    """查询新模型中的非终态订单并推进 Intent/对冲组，返回更新数量。"""
    factory = adapter_factory or _default_adapter_factory
    changed = 0
    with session_factory() as db:
        rows = (
            db.query(VenueOrder, ExecutionLeg, ExecutionIntent)
            .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
            .join(ExecutionIntent, ExecutionIntent.id == ExecutionLeg.intent_id)
            .filter(
                VenueOrder.status.in_(NON_TERMINAL_ORDER_STATUSES),
                ExecutionIntent.status.in_({"RUNNING", "RECOVERY_REQUIRED"}),
            )
            .order_by(VenueOrder.id)
            .limit(max(int(limit), 1))
            .all()
        )
        touched_intents: dict[int, int] = {}
        for venue_order, leg, intent in rows:
            outbox = _outbox_for_leg(db, intent.id, leg.id)
            if outbox is None or outbox.status != "SENT":
                continue
            try:
                snapshot = factory(leg.venue, intent.execution_mode).get_order(
                    leg.venue_symbol,
                    client_order_id=venue_order.client_order_id,
                    venue_order_id=venue_order.venue_order_id,
                )
            except Exception:
                continue
            status = str(getattr(snapshot.status, "value", snapshot.status) or "not_ready").strip().lower()
            if status in {"not_ready", "not_supported", "unknown", ""}:
                continue
            previous = (venue_order.status, venue_order.filled_quantity, venue_order.remaining_quantity)
            _apply_gateway_result(db, outbox, intent, leg, venue_order, snapshot, reconciliation=True)
            current = (venue_order.status, venue_order.filled_quantity, venue_order.remaining_quantity)
            if current != previous:
                changed += 1
            touched_intents[intent.id] = outbox.id
        db.flush()
        for intent_id, outbox_id in touched_intents.items():
            intent = db.get(ExecutionIntent, intent_id)
            outbox = db.get(ExecutionOutbox, outbox_id)
            legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent_id).order_by(ExecutionLeg.id).all()
            if intent is not None and outbox is not None:
                _finish_command(db, outbox, intent, legs, adapter_factory=factory)
        db.commit()
        for intent_id in touched_intents:
            intent = db.get(ExecutionIntent, intent_id)
            if intent is not None:
                _refresh_hedge_pool(db, intent)
    return changed


def _claim_outbox(
    session_factory: sessionmaker,
    *,
    limit: int,
    processing_timeout_seconds: int,
) -> list[ClaimedOutbox]:
    now = utc_now()
    stale_before = now - timedelta(seconds=processing_timeout_seconds)
    with session_factory() as db:
        query = (
            db.query(ExecutionOutbox)
            .filter(
                ExecutionOutbox.available_at <= now,
                (
                    (ExecutionOutbox.status == "PENDING")
                    | ((ExecutionOutbox.status == "PROCESSING") & (ExecutionOutbox.locked_at <= stale_before))
                ),
            )
            .order_by(ExecutionOutbox.id)
            .limit(limit)
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        rows = query.all()
        claims: list[ClaimedOutbox] = []
        for row in rows:
            recovery_only = row.status == "PROCESSING"
            row.status = "PROCESSING"
            row.locked_at = now
            row.attempts = int(row.attempts or 0) + 1
            claims.append(ClaimedOutbox(row.id, recovery_only))
        db.commit()
        return claims


def _process_claim(
    session_factory: sessionmaker,
    adapter_factory: AdapterFactory,
    claim: ClaimedOutbox,
) -> None:
    with session_factory() as db:
        outbox = db.get(ExecutionOutbox, claim.outbox_id)
        if outbox is None or outbox.status != "PROCESSING":
            return
        intent = db.get(ExecutionIntent, outbox.intent_id)
        if intent is None:
            _fail_outbox(db, outbox, None, "Outbox 关联的 ExecutionIntent 不存在")
            db.commit()
            return
        payload = _outbox_payload(outbox)
        if str(payload.get("command_kind") or "").upper() == "CANCEL_ORDER":
            _process_cancel_claim(db, outbox, intent, payload, adapter_factory, recovery_only=claim.recovery_only)
            db.commit()
            _refresh_hedge_pool(db, intent)
            return
        all_legs = (
            db.query(ExecutionLeg)
            .filter(ExecutionLeg.intent_id == intent.id)
            .order_by(ExecutionLeg.id)
            .all()
        )
        legs = _outbox_dispatch_legs(outbox, all_legs)
        if not legs:
            _fail_outbox(db, outbox, intent, "Outbox 没有可投递的执行腿")
            db.commit()
            return
        if claim.recovery_only:
            _recover_without_resubmit(db, outbox, intent, legs, adapter_factory)
            db.commit()
            _refresh_hedge_pool(db, intent)
            return

        intent.status = "RUNNING"
        venue_orders = [_ensure_venue_order(db, intent, leg) for leg in legs]
        commands = [
            _dispatch_command(intent, leg, venue_order)
            for leg, venue_order in zip(legs, venue_orders, strict=True)
        ]
        # 稳定 ClientOrderId 必须先落库，再进行任何外部调用。
        db.commit()

        # 多腿外部调用并行发出，数据库更新仍在当前线程串行完成。
        completed: list[tuple[DispatchCommand, Any]] = []
        failed: list[tuple[DispatchCommand, Exception]] = []
        unknown: list[tuple[DispatchCommand, Exception]] = []
        with ThreadPoolExecutor(max_workers=max(len(commands), 1), thread_name_prefix="execution-leg") as executor:
            futures = {executor.submit(_submit_dispatch_command, adapter_factory, command): command for command in commands}
            for future in as_completed(futures):
                command = futures[future]
                try:
                    completed.append((command, future.result()))
                except Exception as exc:
                    if _exception_outcome_unknown(exc):
                        unknown.append((command, exc))
                    else:
                        failed.append((command, exc))

        outbox = db.get(ExecutionOutbox, claim.outbox_id)
        intent = db.get(ExecutionIntent, outbox.intent_id) if outbox else None
        if outbox is None or intent is None:
            return
        for command, result in completed:
            leg = db.get(ExecutionLeg, command.leg_id)
            venue_order = db.get(VenueOrder, command.venue_order_id)
            if leg is not None and venue_order is not None:
                _apply_gateway_result(db, outbox, intent, leg, venue_order, result)
        for command, exc in failed:
            _persist_dispatch_exception(db, outbox, intent, command, exc, outcome_unknown=False)
        for command, exc in unknown:
            _persist_dispatch_exception(db, outbox, intent, command, exc, outcome_unknown=True)
        db.flush()
        legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
        if unknown:
            detail = "; ".join(
                f"{command.client_order_id}:{_exception_message(exc, outcome_unknown=True)}"
                for command, exc in unknown
            )
            # 外部调用结果未知，只允许之后按稳定 ClientOrderId 查询恢复。
            outbox.status = "PROCESSING"
            outbox.locked_at = utc_now()
            outbox.last_error = f"提交结果未知，等待查询恢复: {detail}"
            intent.status = "RECOVERY_REQUIRED"
            intent.error_message = outbox.last_error
        else:
            _finish_command(db, outbox, intent, legs, adapter_factory=adapter_factory)
        db.commit()
        _refresh_hedge_pool(db, intent)


def _outbox_dispatch_legs(outbox: ExecutionOutbox, all_legs: list[ExecutionLeg]) -> list[ExecutionLeg]:
    """返回当前 Outbox 明确拥有的腿，兼容迁移前未保存 leg IDs 的命令。"""
    payload = _outbox_payload(outbox)
    raw_ids = payload.get("dispatch_leg_ids")
    if not isinstance(raw_ids, list):
        return all_legs
    ids = {int(value) for value in raw_ids if str(value).isdigit()}
    return [leg for leg in all_legs if leg.id in ids]


def _outbox_payload(outbox: ExecutionOutbox) -> dict[str, Any]:
    """安全解析 Outbox 命令载荷。"""
    try:
        payload = json.loads(outbox.payload or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _process_cancel_claim(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    payload: dict[str, Any],
    adapter_factory: AdapterFactory,
    *,
    recovery_only: bool,
) -> None:
    """可靠执行撤单；崩溃恢复只能查询，不能盲目重复发送。"""
    raw_ids = payload.get("dispatch_leg_ids")
    leg_id = int(raw_ids[0]) if isinstance(raw_ids, list) and raw_ids else 0
    leg = db.get(ExecutionLeg, leg_id) if leg_id else None
    order = db.query(VenueOrder).filter(VenueOrder.execution_leg_id == leg_id).one_or_none() if leg else None
    if leg is None or order is None:
        _fail_outbox(db, outbox, intent, "撤单 Outbox 缺少执行腿或 VenueOrder")
        return
    connector = adapter_factory(leg.venue, intent.execution_mode)
    if recovery_only:
        try:
            snapshot = connector.get_order(
                leg.venue_symbol,
                client_order_id=order.client_order_id,
                venue_order_id=order.venue_order_id,
            )
        except Exception as exc:
            outbox.last_error = f"撤单结果未知，等待查询恢复: {exc}"
            intent.status = "RECOVERY_REQUIRED"
            return
        status = str(getattr(snapshot.status, "value", snapshot.status) or "not_ready").strip().lower()
        if status in {"not_ready", "not_supported", "unknown", "", "new", "open", "accepted", "submitted", "partially_filled"}:
            outbox.last_error = "撤单结果尚未确认，继续查询恢复"
            intent.status = "RECOVERY_REQUIRED"
            return
        _apply_gateway_result(db, outbox, intent, leg, order, snapshot, reconciliation=True)
        outbox.status = "SENT"
        outbox.locked_at = None
        legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
        _finish_command(db, outbox, intent, legs, adapter_factory=adapter_factory)
        return
    try:
        snapshot = connector.cancel_order(
            leg.venue_symbol,
            client_order_id=order.client_order_id,
            venue_order_id=order.venue_order_id,
        )
    except Exception as exc:
        # 外部调用可能已到达 venue；保留 PROCESSING，下一轮只允许查询恢复。
        outbox.last_error = f"撤单提交结果未知，等待查询恢复: {exc}"
        intent.status = "RECOVERY_REQUIRED"
        return
    accepted = str(getattr(snapshot.status, "value", snapshot.status)) not in {"REJECTED", "UNKNOWN"}
    if not accepted:
        outbox.status = "FAILED"
        outbox.locked_at = None
        outbox.last_error = f"venue 未接受撤单: {order.client_order_id}"
        order.reconciliation_state = "CANCEL_FAILED"
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = f"Maker TTL 到期但撤单请求失败，禁止市价兜底: {order.client_order_id}"
        return
    outbox.status = "SENT"
    outbox.locked_at = None
    order.status = "PENDING_CANCEL"
    order.reconciliation_state = "CANCEL_REQUESTED"
    intent.status = "RUNNING"


def _outbox_for_leg(db: Session, intent_id: int, leg_id: int) -> ExecutionOutbox | None:
    """找到拥有指定腿的命令，避免动态 Maker 腿误用其他 Outbox。"""
    rows = (
        db.query(ExecutionOutbox)
        .filter(ExecutionOutbox.intent_id == intent_id)
        .order_by(ExecutionOutbox.id.desc())
        .all()
    )
    legacy: ExecutionOutbox | None = None
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        raw_ids = payload.get("dispatch_leg_ids")
        if not isinstance(raw_ids, list):
            legacy = legacy or row
            continue
        if leg_id in {int(value) for value in raw_ids if str(value).isdigit()}:
            return row
    return legacy


def _ensure_venue_order(db: Session, intent: ExecutionIntent, leg: ExecutionLeg) -> VenueOrder:
    row = db.query(VenueOrder).filter(VenueOrder.execution_leg_id == leg.id).one_or_none()
    if row is not None:
        return row
    client_order_id = f"CH-{intent.id}-{leg.id}"
    group = db.get(HedgeGroup, intent.hedge_group_id) if intent.hedge_group_id else None
    legacy_order = Order(
        hedge_group_id=intent.hedge_group_id,
        platform=leg.venue,
        symbol=str(getattr(group, "symbol", "") or leg.instrument_id or leg.venue_symbol),
        side=leg.order_side.lower(),
        quantity=float(leg.strategy_quantity),
        order_type=leg.order_type,
        price=leg.limit_price,
        post_only=bool(leg.post_only),
        # 旧字段仅保留业务开/平分类；是否透传由 venue_reduce_only 决定。
        reduce_only=str(leg.action or "").upper() in {"CLOSE", "COMPENSATE", "FLATTEN"},
        status="initialized",
    )
    db.add(legacy_order)
    db.flush()
    row = VenueOrder(
        execution_leg_id=leg.id,
        legacy_order_id=legacy_order.id,
        client_order_id=client_order_id,
        position_id=(f"{leg.instrument_id}-{leg.position_side}" if leg.position_side in {"LONG", "SHORT"} else ""),
        status="INITIALIZED",
        requested_quantity=float(leg.venue_order_quantity),
        filled_quantity=0.0,
        remaining_quantity=float(leg.venue_order_quantity),
        reconciliation_state="LOCAL",
    )
    db.add(row)
    db.flush()
    return row


def _dispatch_command(intent: ExecutionIntent, leg: ExecutionLeg, venue_order: VenueOrder) -> DispatchCommand:
    return DispatchCommand(
        leg_id=leg.id,
        intent_id=intent.id,
        venue_order_id=venue_order.id,
        venue=leg.venue,
        execution_mode=intent.execution_mode,
        symbol=leg.instrument_id or leg.venue_symbol,
        venue_symbol=leg.venue_symbol,
        side=leg.order_side.lower(),
        quantity=float(leg.venue_order_quantity),
        order_type=leg.order_type,
        price=leg.limit_price,
        post_only=bool(leg.post_only),
        venue_reduce_only=bool(leg.venue_reduce_only),
        position_side=leg.position_side,
        action=leg.action,
        hedge_group_id=intent.hedge_group_id,
        client_order_id=venue_order.client_order_id,
    )


def _submit_dispatch_command(adapter_factory: AdapterFactory, command: DispatchCommand):
    connector = adapter_factory(command.venue, command.execution_mode)
    return connector.submit_order(
        OrderRequest(
            venue=command.venue,
            symbol=command.venue_symbol,
            side=Side(command.side.lower()),
            quantity=Decimal(str(command.quantity)),
            client_order_id=command.client_order_id,
            order_type=OrderType(command.order_type.lower()),
            price=Decimal(str(command.price)) if command.price is not None else None,
            time_in_force=TimeInForce.GTC,
            post_only=command.post_only,
            reduce_only=command.venue_reduce_only,
            position_side=PositionSide(command.position_side) if command.position_side in {"NET", "LONG", "SHORT"} else PositionSide.NET,
            metadata={
                "hedge_group_id": command.hedge_group_id,
                "intent_id": command.intent_id,
                "leg_id": command.leg_id,
                "action": command.action,
            },
        )
    )


def _apply_gateway_result(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    venue_order: VenueOrder,
    result: Any,
    *,
    reconciliation: bool = False,
) -> None:
    raw_status = getattr(result, "status", "unknown")
    status = str(getattr(raw_status, "value", raw_status) or "unknown").upper()
    filled_quantity = max(float(getattr(result, "filled_quantity", 0.0) or 0.0), 0.0)
    requested_quantity = float(venue_order.requested_quantity or leg.venue_order_quantity)
    venue_order.venue_order_id = str(getattr(result, "venue_order_id", "") or venue_order.venue_order_id)
    venue_order.status = status
    venue_order.filled_quantity = max(float(venue_order.filled_quantity or 0.0), filled_quantity)
    venue_order.remaining_quantity = max(requested_quantity - venue_order.filled_quantity, 0.0)
    average_price = float(getattr(result, "average_price", 0.0) or 0.0)
    if average_price > 0:
        venue_order.average_price = average_price
    venue_order.commission = float(getattr(result, "commission", 0.0) or venue_order.commission or 0.0)
    venue_order.reconciliation_state = "RECONCILED" if reconciliation else "VENUE_ACK"
    venue_order.last_event_at = utc_now()
    venue_order.raw_last_report = json.dumps(_result_payload(result), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _sync_legacy_order_and_fills(db, venue_order, result)

    if status in SUCCESS_ORDER_STATUSES or venue_order.remaining_quantity <= 1e-12:
        venue_order.status = "FILLED"
        leg.status = "FILLED"
    elif status in FAILURE_ORDER_STATUSES:
        leg.status = "FAILED"
    elif venue_order.filled_quantity > 0:
        leg.status = "PARTIALLY_FILLED"
    else:
        leg.status = "SUBMITTED"
    _append_execution_event(db, outbox, intent, leg, venue_order, result, reconciliation=reconciliation)
    if status in FAILURE_ORDER_STATUSES:
        message = _result_error_message(result) or f"{leg.venue} 返回订单终态 {status}，未提供详细原因"
        intent.error_message = message
        _record_execution_failure(db, intent, leg, venue_order, message, source="venue_result")


def _sync_legacy_order_and_fills(db: Session, venue_order: VenueOrder, result: Any) -> None:
    """迁移期同步旧 Order/Fill，保证现有 PnL、动作资格和页面读取一致。"""
    if venue_order.legacy_order_id is None:
        return
    order = db.get(Order, venue_order.legacy_order_id)
    if order is None:
        return
    order.status = str(venue_order.status or "unknown").lower()
    order.external_order_id = venue_order.venue_order_id
    if venue_order.average_price is not None:
        order.price = venue_order.average_price
    order.error_message = _result_error_message(result)
    recorded = sum(
        abs(float(quantity or 0.0))
        for (quantity,) in db.query(Fill.quantity).filter(Fill.order_id == order.id).all()
    )
    delta = max(float(venue_order.filled_quantity or 0.0) - recorded, 0.0)
    if delta > 1e-12 and float(venue_order.average_price or 0.0) > 0:
        total_filled = max(float(venue_order.filled_quantity or 0.0), delta)
        fee_delta = float(venue_order.commission or 0.0) * delta / total_filled
        db.add(Fill(
            order_id=order.id,
            platform=order.platform,
            symbol=order.symbol,
            side=order.side,
            quantity=delta,
            price=float(venue_order.average_price),
            fee=fee_delta,
        ))


def _append_execution_event(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    venue_order: VenueOrder,
    result: Any,
    *,
    reconciliation: bool,
) -> None:
    payload = {
        "schema_version": 1,
        "outbox_id": outbox.id,
        "intent_id": intent.id,
        "execution_leg_id": leg.id,
        "client_order_id": venue_order.client_order_id,
        **_result_payload(result),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    event_id = f"worker:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:56]}"
    if db.query(ExecutionEvent).filter(ExecutionEvent.event_id == event_id).count():
        return
    db.add(ExecutionEvent(
        event_id=event_id,
        intent_id=intent.id,
        execution_leg_id=leg.id,
        venue_order_id_ref=venue_order.id,
        event_type=_event_type(venue_order.status),
        client_order_id=venue_order.client_order_id,
        venue_order_id=venue_order.venue_order_id,
        ts_event=utc_now(),
        reconciliation=reconciliation,
        payload=canonical,
        processed_at=utc_now(),
    ))


def _persist_dispatch_exception(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    command: DispatchCommand,
    exc: Exception,
    *,
    outcome_unknown: bool,
) -> None:
    """持久化下单调用异常，避免异常只存在于 Worker 内存或容器标准输出。"""
    leg = db.get(ExecutionLeg, command.leg_id)
    venue_order = db.get(VenueOrder, command.venue_order_id)
    if leg is None or venue_order is None:
        return
    message = _exception_message(exc, outcome_unknown=outcome_unknown)
    status = "UNKNOWN" if outcome_unknown else "REJECTED"
    venue_order.status = status
    venue_order.reconciliation_state = "SUBMIT_UNKNOWN" if outcome_unknown else "SUBMIT_FAILED"
    venue_order.last_event_at = utc_now()
    venue_order.raw_last_report = json.dumps({
        "success": False,
        "status": status.lower(),
        "message": message,
        "exception_type": type(exc).__name__,
        "outcome_unknown": outcome_unknown,
        "client_order_id": command.client_order_id,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    leg.status = "UNKNOWN" if outcome_unknown else "FAILED"
    if venue_order.legacy_order_id is not None:
        order = db.get(Order, venue_order.legacy_order_id)
        if order is not None:
            order.status = "unknown" if outcome_unknown else "failed"
            order.error_message = message
    intent.error_message = message
    _append_execution_event(
        db,
        outbox,
        intent,
        leg,
        venue_order,
        _FailureResult(status=status, error_message=message),
        reconciliation=False,
    )
    _record_execution_failure(
        db,
        intent,
        leg,
        venue_order,
        message,
        source="submit_exception_unknown" if outcome_unknown else "submit_exception",
        exception_type=type(exc).__name__,
    )


@dataclass(frozen=True, slots=True)
class _FailureResult:
    status: str
    error_message: str
    venue_order_id: str = ""
    filled_quantity: float = 0.0
    average_price: float = 0.0
    commission: float = 0.0


def _record_execution_failure(
    db: Session,
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    venue_order: VenueOrder,
    message: str,
    *,
    source: str,
    exception_type: str = "",
) -> None:
    context = json.dumps({
        "source": source,
        "outcome_unknown": source == "submit_exception_unknown",
        "intent_id": intent.id,
        "hedge_group_id": intent.hedge_group_id,
        "leg_id": leg.id,
        "venue_order_id": venue_order.id,
        "client_order_id": venue_order.client_order_id,
        "external_order_id": venue_order.venue_order_id,
        "venue": leg.venue,
        "symbol": leg.venue_symbol,
        "execution_mode": intent.execution_mode,
        "order_status": venue_order.status,
        "exception_type": exception_type,
        "error_message": message,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    db.add(SystemLog(
        level="error",
        category="execution",
        message=f"订单执行失败: Intent #{intent.id} {leg.venue}:{leg.venue_symbol}",
        context=context,
    ))
    logger.error(
        "订单执行失败: intent_id={}, leg_id={}, venue={}, symbol={}, client_order_id={}, source={}, error={}",
        intent.id,
        leg.id,
        leg.venue,
        leg.venue_symbol,
        venue_order.client_order_id,
        source,
        message,
    )


def _exception_outcome_unknown(exc: Exception) -> bool:
    marker = getattr(exc, "outcome_unknown", None)
    if marker is not None:
        return bool(marker)
    # 本地参数/权限错误确定发生在提交之前；其他未分类异常按结果未知处理，避免误重发。
    return not isinstance(exc, (PermissionError, ValueError, LookupError))


def _exception_message(exc: Exception, *, outcome_unknown: bool | None = None) -> str:
    unknown = _exception_outcome_unknown(exc) if outcome_unknown is None else outcome_unknown
    details = [f"{type(exc).__name__}: {exc}"]
    for name in ("code", "status", "retry_after"):
        value = getattr(exc, name, None)
        if value is not None:
            details.append(f"{name}={value}")
    details.append(f"outcome_unknown={unknown}")
    return "; ".join(details)


def _result_error_message(result: Any) -> str:
    direct = str(getattr(result, "error_message", "") or "").strip()
    if direct:
        return direct
    raw = getattr(result, "raw", {}) or {}
    if not isinstance(raw, dict):
        return ""
    for key in ("error_message", "message", "msg", "reject_reason", "rejectReason", "reason", "r"):
        value = str(raw.get(key) or "").strip()
        if value and value.upper() not in {"NONE", "NO_ERROR"}:
            return value
    return ""


def _recover_without_resubmit(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    legs: list[ExecutionLeg],
    adapter_factory: AdapterFactory,
) -> None:
    unresolved: list[str] = []
    for leg in legs:
        venue_order = db.query(VenueOrder).filter(VenueOrder.execution_leg_id == leg.id).one_or_none()
        if venue_order is None:
            unresolved.append(f"{leg.leg_key}:缺少稳定 ClientOrderId")
            continue
        if venue_order.status not in NON_TERMINAL_ORDER_STATUSES:
            continue
        connector = adapter_factory(leg.venue, intent.execution_mode)
        snapshot = connector.get_order(
            leg.venue_symbol,
            client_order_id=venue_order.client_order_id,
            venue_order_id=venue_order.venue_order_id,
        )
        status = str(getattr(snapshot.status, "value", snapshot.status) or "not_ready").lower()
        if status in {"not_ready", "not_supported", "unknown", ""}:
            unresolved.append(f"{leg.leg_key}:{venue_order.client_order_id}:{status or 'unknown'}")
            continue
        _apply_gateway_result(db, outbox, intent, leg, venue_order, snapshot, reconciliation=True)
    if unresolved:
        message = "提交结果无法安全重建，已禁止自动重发: " + "; ".join(unresolved)
        _fail_outbox(db, outbox, intent, message, recovery_required=True)
    else:
        all_legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
        _finish_command(db, outbox, intent, all_legs, adapter_factory=adapter_factory)


def _finish_command(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    legs: list[ExecutionLeg],
    *,
    adapter_factory: AdapterFactory | None = None,
) -> None:
    if intent.intent_type in {"PROBE_ENTRY", "PROBE_EXIT"}:
        from app.execution.probe_runs import advance_probe_intent

        if advance_probe_intent(
            db,
            intent=intent,
            current_outbox=outbox,
            legs=legs,
            adapter_factory=adapter_factory,
        ):
            return
    if str(intent.execution_style or "") == "maker_then_market":
        from app.execution.maker_state import advance_maker_intent

        if advance_maker_intent(
            db,
            intent=intent,
            current_outbox=outbox,
            legs=legs,
            adapter_factory=adapter_factory,
        ):
            return
    statuses = {str(leg.status or "").upper() for leg in legs}
    outbox.status = "SENT"
    outbox.locked_at = None
    if statuses and statuses <= {"FILLED"}:
        intent.status = "COMPLETED"
        intent.completed_at = intent.completed_at or utc_now()
    elif "FAILED" in statuses:
        detail = _intent_failure_detail(db, intent.id)
        if statuses & {"PLANNED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}:
            intent.status = "RECOVERY_REQUIRED"
            intent.error_message = detail or "至少一腿失败且仍有非终态订单，继续对账并禁止重复提交"
        else:
            intent.status = "FAILED"
            intent.error_message = detail or "至少一条执行腿被 venue 拒绝或失败"
            intent.completed_at = intent.completed_at or utc_now()
    else:
        intent.status = "RUNNING"
    _project_hedge_group_state(db, outbox, intent, legs)


def _project_hedge_group_state(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent,
    legs: list[ExecutionLeg],
) -> None:
    """由 Intent/Leg 的事实状态推进 HedgeGroup，不依赖 HTTP 返回值。"""
    if intent.hedge_group_id is None or intent.intent_type not in {"OPEN", "CLOSE", "RECOVER"}:
        return
    group = db.get(HedgeGroup, intent.hedge_group_id)
    if group is None:
        return
    try:
        command = json.loads(outbox.payload or "{}")
    except (TypeError, ValueError):
        command = {}
    reason = str(command.get("reason") or "close intent")
    previous_status = str(command.get("previous_group_status") or "open")
    statuses = {str(leg.status or "").upper() for leg in legs}
    filled_any = (
        db.query(VenueOrder)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .filter(ExecutionLeg.intent_id == intent.id, VenueOrder.filled_quantity > 0)
        .count()
        > 0
    )
    has_nonterminal = bool(statuses & {"PLANNED", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"})
    if intent.intent_type == "OPEN":
        _project_open_group_state(db, intent, group, command, statuses, filled_any, has_nonterminal)
        return
    recovery = intent.intent_type == "RECOVER"
    if statuses and statuses <= {"FILLED"}:
        group.status = "closed"
        group.closed_at = group.closed_at or utc_now()
        group.close_reason = reason
        group.fees = _group_commission_total(db, group.id)
        event_type = "recovery_intent_completed" if recovery else "close_intent_completed"
        _add_group_event_once(db, group.id, event_type, f"Intent #{intent.id} 已确认全部成交")
        try:
            from app.execution.pnl import realized_pnl_from_fills

            realized = realized_pnl_from_fills(db, group)
            if realized is not None:
                group.realized_pnl = realized
                group.unrealized_pnl = 0.0
        except Exception:
            # PnL 投影失败不能篡改已确认的订单成交事实，后续统计任务可重算。
            pass
    elif "FAILED" in statuses:
        if filled_any or has_nonterminal:
            group.status = "manual_intervention"
            group.close_reason = f"平仓 Intent #{intent.id} 存在部分成交、单腿失败或待确认订单，必须执行恢复计划"
        else:
            group.status = previous_status if previous_status in {"open", "open_partial"} else "open"
            group.close_reason = f"平仓 Intent #{intent.id} 未产生成交，已恢复原状态；{intent.error_message}"
        event_type = "recovery_intent_failed" if recovery else "close_intent_failed"
        _add_group_event_once(db, group.id, event_type, group.close_reason)
    else:
        group.status = "closing"
        group.close_reason = f"平仓 Intent #{intent.id} 已提交，等待成交确认"


def _project_open_group_state(
    db: Session,
    intent: ExecutionIntent,
    group: HedgeGroup,
    command: dict[str, Any],
    statuses: set[str],
    filled_any: bool,
    has_nonterminal: bool,
) -> None:
    opportunity_id = int(command.get("opportunity_id") or 0)
    from app.db.models import ArbitrageOpportunity

    opportunity = db.get(ArbitrageOpportunity, opportunity_id) if opportunity_id else None
    if statuses and statuses <= {"FILLED"}:
        group.status = "open"
        group.opened_at = group.opened_at or utc_now()
        group.close_reason = ""
        group.fees = _group_commission_total(db, group.id)
        try:
            from app.execution.pnl import actual_entry_spread_from_fills

            entry_spread = actual_entry_spread_from_fills(db, group)
            if entry_spread is not None:
                group.entry_spread = entry_spread
        except Exception:
            pass
        if opportunity is not None:
            opportunity.status = "executed"
            opportunity.reject_reason = ""
        _add_group_event_once(db, group.id, "open_intent_completed", f"Intent #{intent.id} 已确认全部成交")
    elif "FAILED" in statuses:
        if filled_any or has_nonterminal:
            group.status = "manual_intervention"
            group.close_reason = f"开仓 Intent #{intent.id} 存在单腿成交、失败或待确认订单，必须执行恢复计划"
        else:
            group.status = "failed"
            group.close_reason = f"开仓 Intent #{intent.id} 全部未成交"
        if opportunity is not None:
            opportunity.status = "rejected"
            opportunity.reject_reason = group.close_reason
        _add_group_event_once(db, group.id, "open_intent_failed", group.close_reason)
    else:
        group.status = "opening"
        if opportunity is not None:
            opportunity.status = "executing"
            opportunity.reject_reason = f"OPEN Intent #{intent.id} 等待成交确认"


def _add_group_event_once(db: Session, group_id: int, event_type: str, detail: str) -> None:
    exists = (
        db.query(HedgeGroupEvent.id)
        .filter(HedgeGroupEvent.hedge_group_id == group_id, HedgeGroupEvent.event_type == event_type, HedgeGroupEvent.detail == detail)
        .first()
    )
    if not exists:
        db.add(HedgeGroupEvent(hedge_group_id=group_id, event_type=event_type, detail=detail))


def _group_commission_total(db: Session, group_id: int) -> float:
    """汇总本组所有新执行模型订单的开仓、平仓及恢复手续费。"""
    rows = (
        db.query(VenueOrder.commission)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .join(ExecutionIntent, ExecutionIntent.id == ExecutionLeg.intent_id)
        .filter(ExecutionIntent.hedge_group_id == group_id)
        .all()
    )
    return sum(float(fee or 0.0) for (fee,) in rows)


def _refresh_hedge_pool(db: Session, intent: ExecutionIntent) -> None:
    """事务提交后同步 Redis 快照池，数据库仍是执行状态的权威来源。"""
    if intent.hedge_group_id is None:
        return
    group = db.get(HedgeGroup, intent.hedge_group_id)
    if group is None:
        return
    from app.execution.hedge_pool import hedge_pool

    hedge_pool.upsert_group(group)


def _fail_outbox(
    db: Session,
    outbox: ExecutionOutbox,
    intent: ExecutionIntent | None,
    message: str,
    *,
    recovery_required: bool = False,
) -> None:
    outbox.status = "FAILED"
    outbox.locked_at = None
    outbox.last_error = message
    if intent is not None:
        intent.status = "RECOVERY_REQUIRED" if recovery_required else "FAILED"
        intent.error_message = message


def _default_adapter_factory(venue: str, execution_mode: str) -> Any:
    return native_venue_manager.connector_for(venue, execution_mode)


def _result_payload(result: Any) -> dict[str, Any]:
    raw_status = getattr(result, "status", "unknown")
    return {
        "success": str(getattr(raw_status, "value", raw_status)).upper() not in FAILURE_ORDER_STATUSES,
        "status": str(getattr(raw_status, "value", raw_status) or "unknown").lower(),
        "external_order_id": str(getattr(result, "venue_order_id", "") or ""),
        "filled_quantity": float(getattr(result, "filled_quantity", 0.0) or 0.0),
        "average_price": float(getattr(result, "average_price", 0.0) or 0.0),
        "fee": float(getattr(result, "commission", 0.0) or 0.0),
        "message": _result_error_message(result),
    }


def _intent_failure_detail(db: Session, intent_id: int) -> str:
    rows = (
        db.query(Order.error_message)
        .join(VenueOrder, VenueOrder.legacy_order_id == Order.id)
        .join(ExecutionLeg, ExecutionLeg.id == VenueOrder.execution_leg_id)
        .filter(ExecutionLeg.intent_id == intent_id, Order.error_message != "")
        .order_by(Order.id)
        .all()
    )
    messages = list(dict.fromkeys(str(message or "").strip() for (message,) in rows if message))
    return "; ".join(messages)


def _event_type(status: str) -> str:
    normalized = str(status or "").upper()
    if normalized == "FILLED":
        return "ORDER_FILLED"
    if normalized == "PARTIALLY_FILLED":
        return "ORDER_PARTIALLY_FILLED"
    if normalized in FAILURE_ORDER_STATUSES:
        return "ORDER_REJECTED"
    if normalized in {"ACCEPTED", "OPEN"}:
        return "ORDER_ACCEPTED"
    return "ORDER_SUBMITTED"
