"""独立 ProbeRun 创建、回平与仓位基线确认。

Probe 使用真实最小 venue 数量，绝不借用 Paper 策略数量。入口成交后必须创建新的
PROBE_EXIT Intent，以同一 PositionId 明确反向回平；只有成交残量为零且账户该
仓位侧恢复到运行前基线，Run 才能进入 ``FLAT``。
"""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_CEILING
from typing import Any, Callable

from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import get_settings
from app.core.time_utils import utc_now
from app.db.models import (
    ExecutionEvent,
    ExecutionIntent,
    ExecutionLeg,
    ExecutionOutbox,
    ProbeRun,
    SymbolMapping,
    VenueOrder,
)
from app.db.session import SessionLocal
from app.execution.intents import ExecutionLegPlan, IntentCreationResult, create_execution_intent
from app.execution.runtime_settings import paper_live_probe_enabled_for_venue
from app.venues.domain.models import PositionSide
from app.venues.manager import native_venue_manager


TERMINAL_LEG_STATUSES = {"FILLED", "FAILED"}


def build_market_adapter(venue: str, *, live: bool = True):
    """构建 Probe 使用的原生连接器；保留独立函数便于确定性测试替换。"""
    return native_venue_manager.connector_for(venue, "live" if live else "paper")


def create_probe_run(
    db: Session,
    *,
    symbol: str,
    venue: str,
    side: str,
    purpose: str,
    requested_by: str,
    idempotency_key: str,
) -> tuple[ProbeRun, IntentCreationResult]:
    """创建真实最小量入口 Intent；不在 API 请求线程发送订单。"""
    key = idempotency_key.strip()
    if not key:
        raise ValueError("Probe 请求必须提供 Idempotency-Key")
    existing = db.query(ProbeRun).filter(ProbeRun.idempotency_key == key).one_or_none()
    if existing is not None:
        intent = db.get(ExecutionIntent, existing.entry_intent_id)
        if intent is None:
            raise ValueError(f"ProbeRun #{existing.id} 缺少入口 Intent")
        return existing, IntentCreationResult(intent, False)

    normalized_venue = str(venue or "").strip().lower()
    normalized_side = str(side or "").strip().lower()
    if normalized_venue != "binance":
        raise ValueError("独立 ProbeRun 当前仅开放 Binance；其他 venue 的旧直接探针已禁用")
    if normalized_side not in {"buy", "sell"}:
        raise ValueError("side 必须是 buy 或 sell")
    if not paper_live_probe_enabled_for_venue(db, get_settings(), normalized_venue):
        raise ValueError("Binance Paper Probe 总开关未开启")

    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).one_or_none()
    if mapping is None:
        raise ValueError("品种映射不存在")
    venue_symbol = _mapping_venue_symbol(mapping, normalized_venue)
    connector = build_market_adapter(normalized_venue, live=True)
    legacy_quantity = getattr(connector, "probe_quantity", None)
    if callable(legacy_quantity):
        probe_quantity = float(legacy_quantity(venue_symbol))
    else:
        instrument = connector.get_instrument(venue_symbol)
        ticker = connector.get_ticker(venue_symbol)
        reference_price = ticker.ask if normalized_side == "buy" else ticker.bid
        minimum = instrument.minimum_quantity
        if instrument.minimum_notional > 0 and reference_price > 0:
            minimum = max(minimum, instrument.minimum_notional / reference_price)
        step = instrument.quantity_step
        probe_quantity_decimal = (
            (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
            if step > 0 else minimum
        )
        probe_quantity = float(probe_quantity_decimal)
    if probe_quantity <= 0:
        raise ValueError("交易所返回的最小探针量无效")
    position_side = "LONG" if normalized_side == "buy" else "SHORT"
    baseline = _position_quantity(connector, venue_symbol, position_side)

    run = ProbeRun(
        idempotency_key=key,
        purpose=str(purpose or "CONNECTIVITY").strip().upper(),
        venue=normalized_venue,
        instrument_id=venue_symbol,
        position_side=position_side,
        entry_side=normalized_side.upper(),
        probe_quantity=probe_quantity,
        residual_quantity=0.0,
        baseline_position_quantity=baseline,
        status="CREATED",
    )
    db.add(run)
    db.flush()
    plan = ExecutionLegPlan(
        leg_key="probe_entry",
        role="PROBE_ENTRY",
        venue=normalized_venue,
        instrument_id=venue_symbol,
        venue_symbol=venue_symbol,
        action="PROBE_ENTRY",
        position_side=position_side,
        order_side=normalized_side.upper(),
        strategy_quantity=probe_quantity,
        venue_order_quantity=probe_quantity,
        target_position_quantity_before=baseline,
        target_position_quantity_after=baseline + probe_quantity,
        order_type="market",
        venue_reduce_only=False,
    )
    result = create_execution_intent(
        db,
        intent_type="PROBE_ENTRY",
        execution_mode="live",
        execution_style="simultaneous_market",
        idempotency_key=key,
        requested_by=requested_by,
        legs=[plan],
        command_payload={"probe_run_id": run.id, "probe_stage": "ENTRY"},
    )
    run.entry_intent_id = result.intent.id
    run.status = "OPENING"
    db.flush()
    return run, result


def advance_probe_intent(
    db: Session,
    *,
    intent: ExecutionIntent,
    current_outbox: ExecutionOutbox,
    legs: list[ExecutionLeg],
    adapter_factory: Callable[[str, str], Any] | None,
) -> bool:
    """推进入口或退出 Intent；返回是否接管通用投影。"""
    if intent.intent_type not in {"PROBE_ENTRY", "PROBE_EXIT"}:
        return False
    run = _probe_for_intent(db, intent)
    if run is None or not legs:
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = "Probe Intent 缺少 ProbeRun 或执行腿"
        return True
    if current_outbox.status == "PROCESSING":
        current_outbox.status = "SENT"
        current_outbox.locked_at = None
    leg = legs[0]
    order = db.query(VenueOrder).filter(VenueOrder.execution_leg_id == leg.id).one_or_none()
    if order is None:
        intent.status = "RUNNING"
        return True
    if intent.intent_type == "PROBE_ENTRY":
        _advance_entry(db, run, intent, leg, order)
    else:
        _advance_exit(db, run, intent, leg, order, adapter_factory)
    return True


def reconcile_probe_runs_once(
    *,
    session_factory: sessionmaker = SessionLocal,
    adapter_factory: Callable[[str, str], Any] | None = None,
) -> int:
    """重新验证已成交退出腿的账户基线，供原生事件无适配器时兜底。"""
    changed = 0
    with session_factory() as db:
        runs = db.query(ProbeRun).filter(ProbeRun.status == "FLATTENING", ProbeRun.exit_intent_id.is_not(None)).all()
        for run in runs:
            intent = db.get(ExecutionIntent, run.exit_intent_id)
            if intent is None:
                continue
            legs = db.query(ExecutionLeg).filter(ExecutionLeg.intent_id == intent.id).order_by(ExecutionLeg.id).all()
            outbox = db.query(ExecutionOutbox).filter(ExecutionOutbox.intent_id == intent.id).order_by(ExecutionOutbox.id.desc()).first()
            if not legs or outbox is None:
                continue
            old_status = run.status
            advance_probe_intent(
                db, intent=intent, current_outbox=outbox, legs=legs,
                adapter_factory=adapter_factory,
            )
            changed += int(run.status != old_status)
        db.commit()
    return changed


def probe_run_payload(db: Session, run: ProbeRun) -> dict[str, Any]:
    """返回 ProbeRun 及入口、退出订单的持续成交确认状态。

    不能只返回 ``submitted`` 或 Intent ID。调用方需要直接看到交易所原生事件
    已推进到哪个阶段、累计成交多少、还剩多少，才能区分“已提交等待 ACK”、
    “部分成交等待剩余成交”和“已成交但仍在验证仓位基线”。
    """
    return {
        "id": run.id,
        "hedge_group_id": run.hedge_group_id,
        "purpose": run.purpose,
        "venue": run.venue,
        "instrument_id": run.instrument_id,
        "position_side": run.position_side,
        "entry_side": run.entry_side,
        "probe_quantity": run.probe_quantity,
        "baseline_position_quantity": run.baseline_position_quantity,
        "final_position_quantity": run.final_position_quantity,
        "residual_quantity": run.residual_quantity,
        "status": run.status,
        "error_message": run.error_message,
        "entry_intent_id": run.entry_intent_id,
        "exit_intent_id": run.exit_intent_id,
        "open_venue_order_id": run.open_venue_order_id,
        "close_venue_order_id": run.close_venue_order_id,
        "open_fill_price": run.open_fill_price,
        "close_fill_price": run.close_fill_price,
        "flat_confirmed_at": run.flat_confirmed_at,
        "entry": _probe_stage_payload(db, run.entry_intent_id),
        "exit": _probe_stage_payload(db, run.exit_intent_id),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _probe_stage_payload(db: Session, intent_id: int | None) -> dict[str, Any] | None:
    """汇总单个 Probe 阶段的 Intent、执行腿、订单和最近原生事件。"""
    if intent_id is None:
        return None
    intent = db.get(ExecutionIntent, intent_id)
    if intent is None:
        return {"intent_id": intent_id, "status": "MISSING", "error_message": "Intent 不存在"}
    leg = (
        db.query(ExecutionLeg)
        .filter(ExecutionLeg.intent_id == intent_id)
        .order_by(ExecutionLeg.id)
        .first()
    )
    order = (
        db.query(VenueOrder)
        .filter(VenueOrder.execution_leg_id == leg.id)
        .order_by(VenueOrder.id.desc())
        .first()
        if leg is not None else None
    )
    latest_event = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.venue_order_id_ref == order.id)
        .order_by(ExecutionEvent.ts_event.desc(), ExecutionEvent.id.desc())
        .first()
        if order is not None else None
    )
    return {
        "intent_id": intent.id,
        "status": intent.status,
        "error_message": intent.error_message,
        "leg_status": leg.status if leg is not None else None,
        "role": leg.role if leg is not None else None,
        "order_side": leg.order_side if leg is not None else None,
        "position_side": leg.position_side if leg is not None else None,
        "venue_reduce_only": leg.venue_reduce_only if leg is not None else None,
        "order": None if order is None else {
            "id": order.id,
            "status": order.status,
            "client_order_id": order.client_order_id,
            "venue_order_id": order.venue_order_id,
            "position_id": order.position_id,
            "requested_quantity": order.requested_quantity,
            "filled_quantity": order.filled_quantity,
            "remaining_quantity": order.remaining_quantity,
            "average_price": order.average_price,
            "commission": order.commission,
            "last_event_at": order.last_event_at,
            "reconciliation_state": order.reconciliation_state,
        },
        "latest_event": None if latest_event is None else {
            "event_id": latest_event.event_id,
            "event_type": latest_event.event_type,
            "ts_event": latest_event.ts_event,
            "reconciliation": latest_event.reconciliation,
        },
    }


def _advance_entry(db: Session, run: ProbeRun, intent: ExecutionIntent, leg: ExecutionLeg, order: VenueOrder) -> None:
    filled = float(order.filled_quantity or 0.0)
    run.open_venue_order_id = order.id
    run.open_fill_price = order.average_price
    run.residual_quantity = filled
    if leg.status not in TERMINAL_LEG_STATUSES:
        run.status = "OPENING"
        intent.status = "RUNNING"
        return
    if filled <= 1e-12:
        run.status = "FAILED_NO_EXPOSURE"
        run.error_message = "Probe 入口订单终态且无成交"
        intent.status = "FAILED"
        intent.error_message = run.error_message
        intent.completed_at = intent.completed_at or utc_now()
        return
    intent.status = "COMPLETED"
    intent.completed_at = intent.completed_at or utc_now()
    if run.exit_intent_id is None:
        _create_exit_intent(db, run, leg, filled)


def _create_exit_intent(db: Session, run: ProbeRun, entry_leg: ExecutionLeg, quantity: float) -> None:
    exit_side = "SELL" if run.entry_side == "BUY" else "BUY"
    plan = ExecutionLegPlan(
        leg_key="probe_exit",
        role="PROBE_EXIT",
        parent_leg_id=entry_leg.id,
        venue=run.venue,
        instrument_id=run.instrument_id,
        venue_symbol=run.instrument_id,
        action="PROBE_EXIT",
        position_side=run.position_side,
        order_side=exit_side,
        strategy_quantity=quantity,
        venue_order_quantity=quantity,
        target_position_quantity_before=run.baseline_position_quantity + quantity,
        target_position_quantity_after=run.baseline_position_quantity,
        order_type="market",
        # Binance Hedge Mode 使用 PositionId 反向单；绝不发送 reduce_only。
        venue_reduce_only=run.venue != "binance",
    )
    result = create_execution_intent(
        db,
        intent_type="PROBE_EXIT",
        execution_mode="live",
        execution_style="simultaneous_market",
        idempotency_key=f"{run.idempotency_key}:exit",
        requested_by="probe_state_machine",
        legs=[plan],
        command_payload={"probe_run_id": run.id, "probe_stage": "EXIT"},
    )
    run.exit_intent_id = result.intent.id
    run.status = "FLATTENING"


def _advance_exit(
    db: Session,
    run: ProbeRun,
    intent: ExecutionIntent,
    leg: ExecutionLeg,
    order: VenueOrder,
    adapter_factory: Callable[[str, str], Any] | None,
) -> None:
    filled = float(order.filled_quantity or 0.0)
    run.close_venue_order_id = order.id
    run.close_fill_price = order.average_price
    run.residual_quantity = max(float(run.probe_quantity or 0.0) - filled, 0.0)
    if leg.status not in TERMINAL_LEG_STATUSES:
        run.status = "FLATTENING"
        intent.status = "RUNNING"
        return
    if run.residual_quantity > _tol(run.probe_quantity):
        run.status = "RECOVERY_REQUIRED"
        run.error_message = f"Probe 退出未完全成交，残量 {run.residual_quantity:g}"
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = run.error_message
        return
    if adapter_factory is None:
        run.status = "FLATTENING"
        intent.status = "RUNNING"
        return
    try:
        adapter = adapter_factory(run.venue, "live")
        final_quantity = _position_quantity(adapter, run.instrument_id, run.position_side)
    except Exception as exc:
        run.status = "FLATTENING"
        intent.status = "RUNNING"
        intent.error_message = f"等待 Probe 仓位基线确认: {exc}"
        return
    run.final_position_quantity = final_quantity
    delta = abs(final_quantity - float(run.baseline_position_quantity or 0.0))
    if delta > _tol(run.probe_quantity):
        run.status = "RECOVERY_REQUIRED"
        run.error_message = (
            f"Probe 成交残量为零但账户仓位未恢复基线: "
            f"baseline={run.baseline_position_quantity:g}, final={final_quantity:g}"
        )
        intent.status = "RECOVERY_REQUIRED"
        intent.error_message = run.error_message
        return
    run.status = "FLAT"
    run.residual_quantity = 0.0
    run.flat_confirmed_at = utc_now()
    run.error_message = ""
    intent.status = "COMPLETED"
    intent.completed_at = intent.completed_at or utc_now()


def _probe_for_intent(db: Session, intent: ExecutionIntent) -> ProbeRun | None:
    field = ProbeRun.entry_intent_id if intent.intent_type == "PROBE_ENTRY" else ProbeRun.exit_intent_id
    return db.query(ProbeRun).filter(field == intent.id).one_or_none()


def _mapping_venue_symbol(mapping: SymbolMapping, venue: str) -> str:
    for key in ("a", "b"):
        mapped_venue = str(getattr(mapping, f"leg_{key}_venue", "") or "").lower()
        mapped_symbol = str(getattr(mapping, f"leg_{key}_symbol", "") or "")
        if mapped_venue == venue:
            return mapped_symbol
    raise ValueError(f"品种 {mapping.symbol} 没有 {venue} 执行腿")


def _position_quantity(connector: Any, instrument_id: str, position_side: str) -> float:
    expected_side = PositionSide.LONG if position_side == "LONG" else PositionSide.SHORT
    normalized_symbol = str(instrument_id or "").upper()
    total = 0.0
    for row in connector.get_positions() or []:
        if isinstance(row, dict):
            symbol = str(row.get("symbol") or row.get("instrument_id") or "").upper()
            raw_side = str(row.get("side") or row.get("position_side") or "").upper()
            quantity = row.get("quantity") or row.get("size") or 0
        else:
            symbol = str(row.symbol or "").upper()
            raw_side = str(getattr(row.position_side, "value", row.position_side)).upper()
            quantity = row.quantity
        if normalized_symbol not in symbol and symbol not in normalized_symbol:
            continue
        if raw_side != expected_side.value:
            continue
        total += abs(float(quantity))
    return total


def _tol(quantity: float) -> float:
    return max(abs(float(quantity or 0.0)) * 1e-6, 1e-12)
