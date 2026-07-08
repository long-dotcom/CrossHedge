"""
执行控制路由
============

- POST /execution/reconcile          —— 执行对账
- POST /execution/venue-probe-test   —— Venue 探针测试
- GET  /orders                       —— 订单列表（分页）
- GET  /fills                        —— 成交列表（分页）

注意：/orders 和 /fills 在 /api 根级别，不在 /api/execution 下。
因此本模块导出两个 router：
- ``execution_router``（prefix="/execution"）
- ``orders_fills_router``（无 prefix）
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.base import AdapterOrder
from app.adapters.hyperliquid import HyperliquidAdapter, _load_hyperliquid_exchange
from app.adapters.venue import build_market_adapter, mapping_leg
from app.api.deps import _leg_metadata_for_symbol, _row_with_leg_metadata, audit
from app.auth.dependencies import get_current_user, require_admin
from app.config.settings import get_settings
from app.db.models import Fill, Order, SymbolMapping, User
from app.db.session import get_db
from app.execution.carry_costs import run_carry_cost_sync
from app.execution.reconciler import run_execution_reconcile
from app.schemas import VenueProbeTestIn

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
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Venue 探针测试：模拟或真实提交一笔订单以验证连通性。"""
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

    settings = get_settings()
    adapter = build_market_adapter(venue, live=True)
    adapter.paper_price_probe = True
    slippage = float(payload.slippage if payload.slippage is not None else settings.hyperliquid.paper_live_slippage)

    try:
        if payload.quantity is not None:
            probe_quantity = float(payload.quantity)
        elif venue == "hyperliquid" and hasattr(adapter, "_probe_quantity"):
            probe_quantity = adapter._probe_quantity(venue_symbol)
        else:
            # Nautilus venue 的真实最小下单量由 runtime 在提交时根据 instrument 规格计算；
            # 这里的数量只作为 paper 账本测试数量展示。
            probe_quantity = 1.0
        if venue == "hyperliquid":
            exchange = _load_hyperliquid_exchange(settings)
            slippage_price = exchange._slippage_price(venue_symbol, side == "buy", slippage, None)
        else:
            exchange = None
            slippage_price = 0.0

        response: dict[str, Any] = {
            "symbol": payload.symbol,
            "venue": venue,
            "venue_symbol": venue_symbol,
            "side": side,
            "reduce_only": payload.reduce_only,
            "submit": payload.submit,
            "probe_quantity": probe_quantity,
            "slippage": slippage,
            "slippage_price": slippage_price,
            "asset": exchange.info.coin_to_asset.get(venue_symbol) if exchange else None,
            "status": "dry_run_ok",
        }

        if not payload.submit:
            audit(db, user.id, "venue_probe_dry_run", "execution", f"{venue}:{venue_symbol} {side} {probe_quantity}")
            db.commit()
            return response

        confirmation_phrase = f"SUBMIT {venue.upper()} PROBE"
        if payload.confirmation != confirmation_phrase:
            raise HTTPException(status_code=400, detail=f"真实提交必须传 confirmation='{confirmation_phrase}'")

        order_result = adapter.place_order(
            AdapterOrder(
                platform=venue,
                symbol=payload.symbol,
                venue_symbol=venue_symbol,
                side=side,
                quantity=probe_quantity,
                order_type="market",
                reduce_only=payload.reduce_only,
            )
        )
        response.update({
            "status": order_result.status,
            "success": order_result.success,
            "external_order_id": order_result.external_order_id,
            "filled_quantity": order_result.filled_quantity,
            "average_price": order_result.average_price,
            "fee": order_result.fee,
            "message": order_result.error_message,
        })
        audit(db, user.id, "venue_probe_submit", "execution", f"{venue}:{venue_symbol} {side} {probe_quantity} {order_result.status}")
        db.commit()
        return response

    except HTTPException:
        raise
    except Exception as exc:
        return {
            "symbol": payload.symbol,
            "venue": venue,
            "venue_symbol": venue_symbol,
            "side": side,
            "reduce_only": payload.reduce_only,
            "submit": payload.submit,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
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
