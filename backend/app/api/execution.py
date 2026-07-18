"""
执行控制路由
============

- POST /execution/reconcile          —— 执行对账
- POST /execution/venue-probe-test   —— Venue 探针测试
- POST /execution/intents/{id}/replay —— 从不可变事件重建投影
- GET  /orders                       —— 订单列表（分页）
- GET  /fills                        —— 成交列表（分页）

注意：/orders 和 /fills 在 /api 根级别，不在 /api/execution 下。
因此本模块导出两个 router：
- ``execution_router``（prefix="/execution"）
- ``orders_fills_router``（无 prefix）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.api.deps import _leg_metadata_for_symbol, _row_with_leg_metadata, audit
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import Fill, Order, ProbeRun, SymbolMapping, User
from app.db.session import get_db
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.reconciler import run_execution_reconcile
from app.execution.probe_runs import create_probe_run, probe_run_payload
from app.execution.replay import rebuild_intent_projection
from app.venues.manager import native_venue_manager
from app.schemas import ReplayExecutionIntentIn, VenueProbeTestIn

# 带 /execution 前缀的路由器
execution_router = APIRouter()
# /orders 和 /fills 在 /api 根级别
orders_fills_router = APIRouter()


# ---------------------------------------------------------------------------
# /execution/* 端点
# ---------------------------------------------------------------------------

@execution_router.post("/reconcile")
def execution_reconcile(
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """执行对账：同步 carry cost 并对账待执行订单。"""
    cost_changed = run_carry_cost_sync(db, force=True)
    changed = run_execution_reconcile(db)
    audit(db, user.id, "run_execution_reconcile", "execution", str(changed))
    db.commit()
    return {"status": "ok", "changed": changed, "cost_changed": cost_changed}


@execution_router.post("/venue-probe-test")
def venue_probe_test(
    payload: VenueProbeTestIn,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Venue 探针测试；真实提交只创建可自动回平的独立 ProbeRun。"""
    side = payload.side.lower()
    if side not in {"buy", "sell"}:
        raise HTTPException(status_code=400, detail="side 必须是 buy 或 sell")

    venue = payload.venue or "hyperliquid"
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == payload.symbol).first()
    if mapping:
        leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
        leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
        if venue == leg_a_venue:
            venue_symbol = leg_a_symbol
        elif venue == leg_b_venue:
            venue_symbol = leg_b_symbol
        else:
            venue_symbol = payload.symbol
    else:
        venue_symbol = payload.symbol

    try:
        connector = native_venue_manager.connector_for(venue, "live")
        instrument = connector.get_instrument(venue_symbol)
        ticker = connector.get_ticker(venue_symbol)
        reference_price = ticker.ask if side == "buy" else ticker.bid
        probe_quantity = instrument.minimum_quantity
        if instrument.minimum_notional > 0 and reference_price > 0:
            probe_quantity = max(probe_quantity, instrument.minimum_notional / reference_price)
            steps = (probe_quantity / instrument.quantity_step).to_integral_value(rounding="ROUND_CEILING")
            probe_quantity = steps * instrument.quantity_step
        probe_quantity = float(probe_quantity)

        preview: dict[str, Any] = {
            "symbol": payload.symbol,
            "venue": venue,
            "venue_symbol": venue_symbol,
            "side": side,
            "reduce_only": False,
            "submit": payload.submit,
            "probe_quantity": probe_quantity,
            "status": "dry_run_ok",
            "message": "真实提交将自动创建入口与明确反向退出 Intent，并验证仓位恢复基线",
        }

        if not payload.submit:
            audit(db, user.id, "venue_probe_dry_run", "execution", f"{venue}:{venue_symbol} {side} {probe_quantity}")
            db.commit()
            return preview

        confirmation_phrase = f"SUBMIT {venue.upper()} PROBE"
        if payload.confirmation != confirmation_phrase:
            raise HTTPException(status_code=400, detail=f"真实提交必须传 confirmation='{confirmation_phrase}'")

        if not idempotency_key:
            raise HTTPException(status_code=400, detail="真实 Probe 必须提供 Idempotency-Key")
        if payload.reduce_only:
            raise HTTPException(status_code=400, detail="Probe 不接受客户端 reduce_only；Binance Hedge Mode 由服务端使用明确 PositionId")
        if payload.quantity is not None:
            raise HTTPException(status_code=400, detail="Probe 数量必须由交易所 instrument 最小量自动计算，禁止客户端覆盖")
        run, result = create_probe_run(
            db,
            symbol=payload.symbol,
            venue=venue,
            side=side,
            purpose="CONNECTIVITY",
            requested_by=f"user:{user.id}",
            idempotency_key=idempotency_key,
        )
        audit(db, user.id, "probe_run_created", "execution", f"probe_run={run.id} intent={result.intent.id} {venue}:{venue_symbol} {side}")
        db.commit()
        db.refresh(run)
        response.status_code = 202
        return {**probe_run_payload(db, run), "created": result.created}

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@execution_router.get("/probe-runs/{probe_run_id}")
def get_probe_run(
    probe_run_id: int,
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """读取独立探针的入口、退出、残量与仓位基线确认状态。"""
    run = db.get(ProbeRun, probe_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="ProbeRun 不存在")
    return probe_run_payload(db, run)


@execution_router.post("/intents/{intent_id}/replay")
def replay_execution_intent(
    intent_id: int,
    payload: ReplayExecutionIntentIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """仅重建数据库投影；不创建 Outbox，也不向任何交易场所发送命令。"""
    confirmation = f"REPLAY {intent_id}"
    if payload.confirmation != confirmation:
        raise HTTPException(status_code=400, detail=f"重建必须传 confirmation='{confirmation}'")
    try:
        result = rebuild_intent_projection(db, intent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(
        db,
        user.id,
        "replay_execution_intent",
        "execution_intent",
        f"intent={result.intent_id} events={result.event_count} orders={result.order_count} status={result.intent_status}",
    )
    db.commit()
    return {
        "status": "ok",
        "intent_id": result.intent_id,
        "event_count": result.event_count,
        "order_count": result.order_count,
        "intent_status": result.intent_status,
        "external_commands_created": 0,
    }


# ---------------------------------------------------------------------------
# /orders 和 /fills（/api 根级别）
# ---------------------------------------------------------------------------

@orders_fills_router.get("/orders")
def orders(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """订单列表（分页）。"""
    return _orders_payload(db, page=page, page_size=page_size)


def _orders_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """组装订单列表。"""
    query = db.query(Order)
    total = query.count()
    rows = query.order_by(desc(Order.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [_row_with_leg_metadata(db, r) for r in rows]}


@orders_fills_router.get("/fills")
def fills(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """成交列表（分页）。"""
    return _fills_payload(db, page=page, page_size=page_size)


def _fills_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """组装成交列表。"""
    query = db.query(Fill)
    total = query.count()
    rows = query.order_by(desc(Fill.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [_row_with_leg_metadata(db, r) for r in rows]}
