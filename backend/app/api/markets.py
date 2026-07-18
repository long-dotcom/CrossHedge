"""
行情与市场路由
==============

- POST /markets/scan                       —— 手动触发扫描
- GET  /markets/symbols                    —— 品种映射列表
- GET  /markets/quotes                     —— 最新报价
- GET  /markets/trading-sessions           —— MT5 交易会话
- GET  /markets/spreads                    —— 当前价差（分页）
- POST /markets/spreads/{symbol}/execute   —— 从价差直接创建机会并执行
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.api.deps import (
    _enabled_symbol_names,
    _leg_metadata,
    _leg_metadata_for_symbol,
    _paginate_rows,
    _row_with_leg_metadata,
)
from app.auth.dependencies import get_current_user, require_admin
from app.db.models import (
    ArbitrageOpportunity,
    SpreadCurrent,
    SpreadDirectionCurrent,
    SymbolMapping,
    User,
)
from app.db.session import get_db
from app.execution.coordinator import create_open_intent
from app.market.quotes import quote_cache
from app.market.scan_state import scan_state_store
from app.market.scanner import run_scan
from app.market.mt5_sessions import as_session_dict, mt5_session_state
from app.adapters.venue import mapping_leg

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助：从价差创建套利机会
# ---------------------------------------------------------------------------

def _current_row_notional(row: SpreadDirectionCurrent, mapping: SymbolMapping) -> float:
    """根据价差行和策略设置估算名义价值。"""
    configured = float(mapping.target_notional or 0.0)
    leg_a_mid = (float(row.leg_a_bid or 0.0) + float(row.leg_a_ask or 0.0)) / 2
    estimated = leg_a_mid * float(row.leg_a_quantity or row.quantity or 0.0)
    return max(configured, estimated, 0.0)


def _create_current_symbol_opportunity(
    db: Session,
    symbol: str,
    source: str,
    *,
    direction: str = "",
    force: bool = False,
) -> ArbitrageOpportunity:
    """从 SpreadDirectionCurrent 创建一条 ArbitrageOpportunity 记录。"""
    normalized = symbol.strip().upper()
    if not normalized:
        raise HTTPException(status_code=400, detail="symbol 不能为空")
    mapping = db.query(SymbolMapping).filter(
        SymbolMapping.symbol == normalized,
        SymbolMapping.enabled.is_(True),
    ).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="品种映射不存在或未启用")

    query = db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol == normalized)
    if direction.strip():
        query = query.filter(SpreadDirectionCurrent.direction == direction.strip())
    rows = query.order_by(
        desc(SpreadDirectionCurrent.net_profit),
        desc(SpreadDirectionCurrent.updated_at),
    ).all()

    executable = [r for r in rows if r.status == "executable"]
    if not executable and not force:
        reason = rows[0].reason if rows else "当前品种没有方向快照"
        status = rows[0].status if rows else "missing"
        raise HTTPException(
            status_code=400,
            detail=f"当前品种没有 executable 方向，最新状态 {status}: {reason}",
        )
    if not executable and not rows:
        raise HTTPException(status_code=400, detail="当前品种没有方向快照")

    row = executable[0] if executable else rows[0]
    opportunity = ArbitrageOpportunity(
        symbol=normalized,
        direction=row.direction,
        notional=_current_row_notional(row, mapping),
        quantity=row.quantity,
        leg_b_quantity=row.leg_b_quantity or row.quantity,
        leg_a_quantity=row.leg_a_quantity or row.quantity,
        notional_currency=row.notional_currency,
        fx_rate_to_usd=row.fx_rate_to_usd,
        gross_spread=row.gross_spread,
        trigger_leg_a_bid=row.leg_a_bid,
        trigger_leg_a_ask=row.leg_a_ask,
        trigger_leg_b_bid=row.leg_b_bid,
        trigger_leg_b_ask=row.leg_b_ask,
        unit_cost=row.unit_cost,
        unit_net_profit=row.unit_net_profit,
        total_cost=row.total_cost,
        net_profit=row.net_profit,
        annualized_return=row.annualized_return,
        entry_threshold=row.entry_spread,
        exit_target=row.close_spread,
        overheat_threshold=0.0,
        signal_sample_count=0,
        status="executable",
        reject_reason=(
            f"{'manual_force_execute_from_spread' if force else 'manual_execute_from_spread'}"
            f":{source}; source_status={row.status}; source_reason={row.reason}"
        ),
    )
    db.add(opportunity)
    db.flush()
    return opportunity


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.post("/scan")
def scan(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """手动触发一次全品种扫描。"""
    return {"created": run_scan(db)}


@router.get("/symbols")
def market_symbols(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """返回所有品种映射（含双腿元信息）。"""
    return [
        _row_with_leg_metadata(db, row)
        for row in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()
    ]


@router.get("/quotes")
def market_quotes(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """返回所有品种的最新报价。"""
    rows = []
    for mapping in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all():
        for leg in ("a", "b"):
            platform, venue_symbol = mapping_leg(mapping, leg)
            quote = quote_cache.latest(platform, mapping.symbol)
            if quote:
                rows.append({
                    "platform": platform,
                    "symbol": mapping.symbol,
                    "venue_symbol": venue_symbol,
                    **_leg_metadata(mapping),
                    "leg": leg,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "depth_notional": quote.depth_notional,
                    "source": quote.source,
                    "sequence": quote.sequence,
                    "local_recv_ts": quote.local_recv_ts,
                    "exchange_ts": quote.exchange_ts,
                })
    return rows


@router.get("/trading-sessions")
def trading_sessions(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """返回所有品种的 MT5 交易会话状态。"""
    rows = db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()
    return [as_session_dict(mt5_session_state(row)) for row in rows]


@router.get("/spreads")
def spreads(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    page: int = 1,
    page_size: int = 20,
    symbol: str = "",
) -> dict[str, Any]:
    """当前价差列表（分页）。优先使用 Redis 中的扫描状态。"""
    enabled_symbols = _enabled_symbol_names(db)
    state = scan_state_store.snapshot()
    if state["ready"]:
        rows = [
            r for r in state["spreads"]
            if str(r.get("symbol", "")).upper() in enabled_symbols
        ]
        if symbol:
            needle = symbol.upper()
            rows = [r for r in rows if needle in str(r.get("symbol", "")).upper()]
        rows = sorted(rows, key=lambda r: str(r.get("symbol", "")))
        return _paginate_rows([_row_with_leg_metadata(db, r) for r in rows], page, page_size)

    query = db.query(SpreadCurrent)
    query = query.filter(SpreadCurrent.symbol.in_(enabled_symbols)) if enabled_symbols else query.filter(False)
    if symbol:
        query = query.filter(SpreadCurrent.symbol.contains(symbol.upper()))
    total = query.count()
    rows = query.order_by(SpreadCurrent.symbol).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "items": [_row_with_leg_metadata(db, r) for r in rows]}


@router.post("/spreads/{symbol}/execute", status_code=202)
def execute_current_symbol(
    symbol: str,
    direction: str = "",
    force: bool = False,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
    idempotency_key: str = Header(default="", alias="Idempotency-Key"),
) -> dict[str, Any]:
    """从当前价差创建机会和异步 OPEN Intent。"""
    opportunity = _create_current_symbol_opportunity(
        db, symbol, user.username, direction=direction, force=force,
    )
    try:
        result = create_open_intent(
            db,
            opportunity_id=opportunity.id,
            requested_by=f"user:{user.id}",
            idempotency_key=idempotency_key,
            source=user.username,
            force_strategy_checks=force,
        )
        db.commit()
        from app.api.deps import as_dict
        return {"accepted": True, "created": result.created, "intent": as_dict(result.intent)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
