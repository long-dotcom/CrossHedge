"""
SSE 流路由
===========

- GET /stream —— Server-Sent Events 端点，推送实时快照

SSE 端点不走 FastAPI Depends 认证，而是从请求头手动提取 Bearer Token，
因为 EventSource API 不支持自定义 Header。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.accounts.sync import latest_account_snapshots
from app.api.deps import (
    _enabled_symbol_names,
    _leg_metadata_for_symbol,
    _rows_with_leg_metadata,
    _leg_metadata_by_symbol,
    as_dict,
    bearer_token_from_request,
    json_default,
)
from app.auth.security import decode_access_token
from app.config.settings import get_settings
from app.db.models import (
    AccountSnapshot,
    Alert,
    ArbitrageOpportunity,
    Fill,
    HedgeGroup,
    Order,
    RiskEvent,
    RiskSetting,
    SpreadBucket,
    SpreadCurrent,
    SystemLog,
    User,
)
from app.db.session import SessionLocal, get_db
from app.diagnostics.pipeline import build_pipeline_diagnostics
from app.market.scan_state import scan_state_store

router = APIRouter()

# 同一 channel/分页参数的所有客户端共享序列化快照，避免每个 SSE 连接重复查库。
class _SnapshotCacheCompat:
    """保留测试清理入口，实际快照存储位于 Redis。"""

    def clear(self) -> None:
        from app.core.redis_client import redis_client, redis_key
        client = redis_client()
        keys = list(client.scan_iter(match=redis_key("cache", "stream-snapshots", "*")))
        if keys:
            client.delete(*keys)


_snapshot_cache = _SnapshotCacheCompat()
_snapshot_locks: dict[str, threading.Lock] = {}
_snapshot_locks_guard = threading.Lock()


# ---------------------------------------------------------------------------
# 内部辅助：各 channel 的 payload 组装
# ---------------------------------------------------------------------------

def _hedge_groups_payload(db: Session, page: int = 1, page_size: int = 20, include_voided: bool = False) -> dict[str, Any]:
    """对冲组列表（SSE 内部版本）。"""
    from app.execution.hedge_pool import hedge_pool
    from app.api.hedge_groups import _hedge_group_payload
    query = db.query(HedgeGroup)
    if not include_voided:
        query = query.filter(HedgeGroup.status != "voided")
    total = query.count()
    rows = query.order_by(desc(HedgeGroup.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    metadata = _leg_metadata_by_symbol(db, {row.symbol for row in rows})
    active_by_id = {s.id: s for s in hedge_pool.snapshot_groups()}
    items = []
    for row in rows:
        snapshot = active_by_id.get(row.id)
        selected = snapshot if snapshot and snapshot.symbol == row.symbol else row
        items.append(_hedge_group_payload(db, selected, metadata.get(row.symbol.upper())))
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def _positions_payload(db: Session) -> list[dict[str, Any]]:
    """持仓列表（SSE 内部版本）。"""
    from app.positions.live import live_positions_payload

    return live_positions_payload(db)


def _latest_accounts_payload(db: Session) -> list[dict[str, Any]]:
    """最新账户快照（SSE 内部版本）。"""
    return [as_dict(r) for r in latest_account_snapshots(db)]


def _orders_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """订单列表（SSE 内部版本）。"""
    query = db.query(Order)
    total = query.count()
    rows = query.order_by(desc(Order.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": _rows_with_leg_metadata(db, rows)}


def _fills_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """成交列表（SSE 内部版本）。"""
    query = db.query(Fill)
    total = query.count()
    rows = query.order_by(desc(Fill.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": _rows_with_leg_metadata(db, rows)}


def _logs_payload(db: Session, page: int = 1, page_size: int = 20, level: str = "", keyword: str = "") -> dict[str, Any]:
    """系统日志（SSE 内部版本）。"""
    query = db.query(SystemLog)
    if level:
        query = query.filter(SystemLog.level == level)
    if keyword:
        query = query.filter(SystemLog.message.contains(keyword))
    total = query.count()
    rows = query.order_by(desc(SystemLog.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


def _alerts_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """告警列表（SSE 内部版本）。"""
    query = db.query(Alert)
    total = query.count()
    rows = query.order_by(desc(Alert.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


def _risk_status_payload(db: Session) -> dict[str, Any]:
    """风控状态（SSE 内部版本）。"""
    risk = db.query(RiskSetting).first()
    return as_dict(risk) if risk else {}


def _risk_events_payload(db: Session, page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """风控事件（SSE 内部版本）。"""
    query = db.query(RiskEvent)
    total = query.count()
    rows = query.order_by(desc(RiskEvent.created_at)).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "items": [as_dict(r) for r in rows]}


def _lead_lag_payload(
    db: Session,
    symbol: str,
    window_seconds: int,
    threshold_bps: float,
    min_move: float,
    follow_ratio: float,
    max_lag_ms: int,
) -> dict[str, Any]:
    """领先-滞后分析（SSE 内部版本）。"""
    from app.analytics.lead_lag import lead_lag_report
    from app.api.deps import _leg_metadata
    from app.db.models import SymbolMapping
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol.upper()).first()
    leg_meta = _leg_metadata(mapping)
    data = lead_lag_report(
        symbol, window_seconds, threshold_bps, min_move, follow_ratio, max_lag_ms,
        leg_a_venue=leg_meta["leg_a_venue"],
        leg_b_venue=leg_meta["leg_b_venue"],
    )
    data.update(leg_meta)
    return data


def _dashboard_summary_payload(db: Session) -> dict[str, Any]:
    """仪表盘摘要（SSE 内部版本）。"""
    from app.api.dashboard import _dashboard_summary_payload as _impl
    return _impl(db)


def _equity_curve_payload(db: Session) -> list[dict[str, Any]]:
    """权益曲线（SSE 内部版本）。"""
    from app.api.dashboard import _equity_curve_payload as _impl
    return _impl(db)


# ---------------------------------------------------------------------------
# 核心：快照生成器
# ---------------------------------------------------------------------------

def _stream_snapshot(
    db: Session,
    *,
    channel: str = "all",
    page: int = 1,
    page_size: int = 20,
    fill_page: int = 1,
    alert_page: int = 1,
    symbol: str = "JP225",
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
    include_voided: bool = False,
) -> dict[str, Any]:
    """根据 channel 参数生成快照数据。"""
    if channel == "pipeline":
        return {"pipeline": build_pipeline_diagnostics(db)}
    if channel == "hedge-groups":
        return {"hedge_groups": _hedge_groups_payload(db, page=page, page_size=page_size, include_voided=include_voided)}
    if channel == "positions":
        return {"positions": _positions_payload(db)}
    if channel == "accounts":
        return {"accounts": _latest_accounts_payload(db)}
    if channel == "execution":
        return {"orders": _orders_payload(db, page=page, page_size=page_size), "fills": _fills_payload(db, page=fill_page, page_size=page_size)}
    if channel == "dashboard":
        return {"dashboard_summary": _dashboard_summary_payload(db), "equity_curve": _equity_curve_payload(db)}
    if channel == "logs":
        return {"logs": _logs_payload(db, page=page, page_size=page_size), "alerts": _alerts_payload(db, page=alert_page, page_size=page_size)}
    if channel == "risk":
        return {"risk_status": _risk_status_payload(db), "risk_events": _risk_events_payload(db, page=page, page_size=page_size)}
    if channel == "lead-lag":
        return {"lead_lag": _lead_lag_payload(db, symbol, window_seconds, threshold_bps, min_move, follow_ratio, max_lag_ms)}

    # 默认 channel == "all"：聚合价差、机会、账户、Pipeline
    state = scan_state_store.snapshot()
    enabled_symbols = _enabled_symbol_names(db)
    if state["ready"]:
        spread_rows = [r for r in state["spreads"] if str(r.get("symbol", "")).upper() in enabled_symbols]
        opportunity_rows = [r for r in state["opportunities"] if str(r.get("symbol", "")).upper() in enabled_symbols]
        spreads_payload = {"total": len(spread_rows), "items": _rows_with_leg_metadata(db, spread_rows)}
        opportunities_payload = {"total": len(opportunity_rows), "items": _rows_with_leg_metadata(db, opportunity_rows)}
    else:
        spread_rows = db.query(SpreadCurrent).filter(SpreadCurrent.symbol.in_(enabled_symbols)).order_by(SpreadCurrent.symbol).all() if enabled_symbols else []
        opportunity_rows = (
            db.query(ArbitrageOpportunity)
            .filter(
                ArbitrageOpportunity.symbol.in_(enabled_symbols),
                ArbitrageOpportunity.status.in_(["candidate", "executable", "executing"]),
            )
            .order_by(desc(ArbitrageOpportunity.updated_at))
            .limit(50)
            .all()
            if enabled_symbols
            else []
        )
        spreads_payload = {"total": len(spread_rows), "items": _rows_with_leg_metadata(db, spread_rows)}
        opportunities_payload = {"total": len(opportunity_rows), "items": _rows_with_leg_metadata(db, opportunity_rows)}

    account_rows = latest_account_snapshots(db)
    latest_bucket = db.query(SpreadBucket).order_by(desc(SpreadBucket.id)).first()
    return {
        "spreads": spreads_payload,
        "opportunities": opportunities_payload,
        "accounts": [as_dict(r) for r in account_rows],
        "latest_bucket_id": latest_bucket.id if latest_bucket else 0,
        "pipeline": build_pipeline_diagnostics(db),
    }


def _snapshot_cache_key(**params: Any) -> str:
    """生成稳定缓存键，隔离不同 channel、分页和筛选参数。"""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def _cached_stream_event(cache_seconds: float, **params: Any) -> str:
    """在线程中生成并缓存序列化快照；同键并发只允许一个生产者。"""
    import hashlib
    from app.core.redis_client import redis_client, redis_key
    logical_key = _snapshot_cache_key(**params)
    key = redis_key("cache", "stream-snapshots", hashlib.sha256(logical_key.encode()).hexdigest())
    cached = redis_client().get(key)
    if cached is not None:
        return cached
    with _snapshot_locks_guard:
        lock = _snapshot_locks.setdefault(key, threading.Lock())
    with lock:
        cached = redis_client().get(key)
        if cached is not None:
            return cached
        session = SessionLocal()
        try:
            snapshot = _stream_snapshot(session, **params)
            serialized = json.dumps(snapshot, default=json_default, separators=(",", ":"))
        finally:
            session.close()
        redis_client().set(key, serialized, px=max(int(cache_seconds * 1000), 1))
        # 锁仅用于当前进程内避免重复生产，不承载缓存数据。
        with _snapshot_locks_guard:
            if len(_snapshot_locks) > 256:
                for stale_key in list(_snapshot_locks)[: len(_snapshot_locks) - 256]:
                    _snapshot_locks.pop(stale_key, None)
        return serialized


# ---------------------------------------------------------------------------
# 路由端点
# ---------------------------------------------------------------------------

@router.get("")
async def stream(
    request: Request,
    channel: str = "all",
    page: int = 1,
    page_size: int = 20,
    fill_page: int = 1,
    alert_page: int = 1,
    symbol: str = "JP225",
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
    include_voided: bool = False,
) -> StreamingResponse:
    """SSE 实时推送端点。"""
    page = max(int(page), 1)
    fill_page = max(int(fill_page), 1)
    alert_page = max(int(alert_page), 1)
    page_size = min(max(int(page_size), 1), 100)
    window_seconds = min(max(int(window_seconds), 1), 86400)
    max_lag_ms = min(max(int(max_lag_ms), 1), 60000)
    # SSE 不走 Depends，手动验证 Token
    token = bearer_token_from_request(request)
    try:
        payload = decode_access_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="登录已失效") from exc
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == payload.get("sub"), User.is_active.is_(True)).first()
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在或已禁用")
    finally:
        db.close()

    async def event_generator():
        interval = max(get_settings().quote.stream_interval_ms, 250) / 1000
        cache_seconds = max(interval * 0.8, 0.2)
        params = {
            "channel": channel,
            "page": page,
            "page_size": page_size,
            "fill_page": fill_page,
            "alert_page": alert_page,
            "symbol": symbol,
            "window_seconds": window_seconds,
            "threshold_bps": threshold_bps,
            "min_move": min_move,
            "follow_ratio": follow_ratio,
            "max_lag_ms": max_lag_ms,
            "include_voided": include_voided,
        }
        while True:
            try:
                serialized = await asyncio.to_thread(_cached_stream_event, cache_seconds, **params)
                yield f"event: snapshot\ndata: {serialized}\n\n"
            except asyncio.CancelledError:
                break
            await asyncio.sleep(interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
