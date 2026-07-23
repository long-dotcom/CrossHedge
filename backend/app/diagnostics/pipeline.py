"""
管线诊断模块
============

构建完整的交易管线（pipeline）诊断视图，展示从品种映射 → 报价 → 同步 → 扫描 → 信号 → 候选 → 推送
的全链路状态，以及对冲池（hedge pool）中各活跃对冲组的状态。

主要功能：
- 构建每个品种的管线节点（node）和边（edge）状态
- 检测管线阻塞原因（blockers）
- 对冲池分组与阶段管理
- 汇总统计

使用方式::

    from app.diagnostics.pipeline import build_pipeline_diagnostics
    result = build_pipeline_diagnostics(db)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import ArbitrageOpportunity, HedgeGroup, SpreadCurrent, SymbolMapping
from app.execution.hedge_pool import HedgeGroupSnapshot, hedge_pool
from app.execution.pnl import liquidation_pnl_from_close_spread
from app.market.hedge_spreads import hedge_group_spreads
from app.market.mt5_sessions import mt5_session_state
from app.market.quotes import quote_cache
from app.market.scan_state import scan_state_store


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 活跃对冲组状态集合
ACTIVE_GROUP_STATUSES = {"pending_open", "opening", "open", "open_partial", "closing", "manual_intervention"}

# 对冲池阶段排序（用于前端泳道排序）
POOL_STAGE_ORDER = {
    "pending": 0,
    "opening": 1,
    "open": 2,
    "ready_to_close": 3,
    "closing": 4,
    "manual": 5,
}

# 活跃机会状态集合
ACTIVE_OPPORTUNITY_STATUSES = {"candidate", "executable", "executing"}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def build_pipeline_diagnostics(db: Session) -> dict[str, Any]:
    """构建完整的管线诊断数据。

    返回包含以下字段的字典：
    - generated_at: 生成时间
    - summary: 汇总统计
    - symbols: 各品种的管线状态列表
    - pool: 对冲池状态
    """
    now = utc_now()
    settings = get_settings()
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).order_by(SymbolMapping.symbol).all()
    current_by_symbol = _current_spreads(db, mappings)
    opportunities_by_symbol = _active_opportunities(db, mappings)
    groups = _active_groups(db)

    symbols = [
        _symbol_pipeline(
            db,
            mapping,
            current_by_symbol.get(mapping.symbol),
            opportunities_by_symbol.get(mapping.symbol, []),
            now,
            settings,
        )
        for mapping in mappings
    ]
    pool = _pool_payload(groups, now, {m.symbol: m for m in mappings})
    summary = _summary(symbols, pool)
    return {
        "generated_at": now,
        "summary": summary,
        "symbols": symbols,
        "pool": pool,
    }


# ---------------------------------------------------------------------------
# 数据查询
# ---------------------------------------------------------------------------

def _current_spreads(db: Session, mappings: list[SymbolMapping]) -> dict[str, dict[str, Any]]:
    """获取已启用品种的当前价差状态（优先从 Redis 快照读取）"""
    enabled = {row.symbol.upper() for row in mappings}
    state = scan_state_store.snapshot()
    if state["ready"]:
        return {
            str(row.get("symbol", "")).upper(): row
            for row in state["spreads"]
            if str(row.get("symbol", "")).upper() in enabled
        }
    rows = db.query(SpreadCurrent).filter(SpreadCurrent.symbol.in_(enabled)).all() if enabled else []
    return {row.symbol.upper(): _model_dict(row) for row in rows}


def _active_opportunities(db: Session, mappings: list[SymbolMapping]) -> dict[str, list[dict[str, Any]]]:
    """获取已启用品种的活跃交易机会"""
    enabled = {row.symbol.upper() for row in mappings}
    state = scan_state_store.snapshot()
    if state["ready"]:
        rows = [
            row
            for row in state["opportunities"]
            if str(row.get("symbol", "")).upper() in enabled and str(row.get("status", "")) in ACTIVE_OPPORTUNITY_STATUSES
        ]
    else:
        rows = [
            _model_dict(row)
            for row in db.query(ArbitrageOpportunity)
            .filter(ArbitrageOpportunity.symbol.in_(enabled), ArbitrageOpportunity.status.in_(ACTIVE_OPPORTUNITY_STATUSES))
            .order_by(desc(ArbitrageOpportunity.updated_at))
            .limit(50)
            .all()
        ] if enabled else []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("symbol", "")).upper(), []).append(row)
    return grouped


def _active_groups(db: Session) -> list[HedgeGroup]:
    """获取所有活跃对冲组（优先从 Redis 快照池读取）"""
    pending = (
        db.query(HedgeGroup)
        .filter(HedgeGroup.status == "pending_open")
        .order_by(HedgeGroup.symbol.asc(), HedgeGroup.id.asc())
        .limit(30)
        .all()
    )
    pool_groups = [group for group in hedge_pool.snapshot_groups() if group.status in ACTIVE_GROUP_STATUSES]
    if pool_groups:
        return [*pending, *pool_groups]
    return (
        db.query(HedgeGroup)
        .filter(HedgeGroup.status.in_(ACTIVE_GROUP_STATUSES))
        .order_by(HedgeGroup.symbol.asc(), HedgeGroup.id.asc())
        .limit(30)
        .all()
    )


# ---------------------------------------------------------------------------
# 品种管线构建
# ---------------------------------------------------------------------------

def _symbol_pipeline(
    db: Session,
    mapping: SymbolMapping,
    current: dict[str, Any] | None,
    opportunities: list[dict[str, Any]],
    now: datetime,
    settings,
) -> dict[str, Any]:
    """构建单个品种的完整管线诊断数据。

    包含节点（node）状态、边（edge）延迟、指标汇总和阻塞原因。
    """
    symbol = mapping.symbol.upper()
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
    leg_a_quote = quote_cache.latest(leg_a_venue, symbol)
    leg_b_quote = quote_cache.latest(leg_b_venue, symbol)
    session = mt5_session_state(mapping)

    # 报价时效性
    leg_a_age = _age_ms(now, leg_a_quote.local_recv_ts) if leg_a_quote else None
    leg_b_age = _age_ms(now, leg_b_quote.local_recv_ts) if leg_b_quote else None
    sync_diff = (
        abs((leg_a_quote.local_recv_ts - leg_b_quote.local_recv_ts).total_seconds() * 1000)
        if leg_a_quote and leg_b_quote
        else None
    )
    # 各环节耗时指标
    scan_age = _age_ms(now, current.get("sampled_at")) if current else None
    quote_sync_duration = _metric_ms(current, "quote_sync_duration_ms")
    scan_duration = _metric_ms(current, "symbol_scan_duration_ms")
    signal_duration = _metric_ms(current, "signal_duration_ms")
    candidate_sync_duration = _metric_ms(current, "candidate_sync_duration_ms")
    cost_duration = _metric_ms(current, "cost_duration_ms")
    sizing_duration = _metric_ms(current, "sizing_duration_ms")
    persist_duration = _metric_ms(current, "persist_duration_ms")
    opportunity = _best_opportunity(opportunities)
    blocked_stage, status, reason = _pipeline_status(current, leg_a_quote, leg_b_quote, session, leg_a_age, leg_b_age, sync_diff, settings, leg_a_venue, leg_b_venue)
    blockers = _pipeline_blockers(current, leg_a_quote, leg_b_quote, session, leg_a_age, leg_b_age, sync_diff, settings, leg_a_venue, leg_b_venue)

    return {
        "symbol": symbol,
        "leg_a_venue_symbol": mapping.leg_a_venue_symbol,
        "mt5_symbol": mapping.mt5_symbol,
        "leg_a_venue": leg_a_venue,
        "leg_a_symbol": leg_a_symbol,
        "leg_b_venue": leg_b_venue,
        "leg_b_symbol": leg_b_symbol,
        "status": status,
        "blocked_stage": blocked_stage,
        "reason": reason,
        "blockers": blockers,
        "nodes": [
            _node("mapping", "映射", "pass", "已启用"),
            _node(
                "leg_a_quote",
                f"{_venue_label(leg_a_venue)}报价",
                _venue_quote_status(leg_a_venue, leg_a_quote, leg_a_age, session, settings.quote.stale_ms),
                _venue_quote_message(leg_a_venue, leg_a_quote, leg_a_age, session),
                age_ms=leg_a_age,
                source=getattr(leg_a_quote, "source", "") if leg_a_quote else "",
                bid=getattr(leg_a_quote, "bid", None) if leg_a_quote else None,
                ask=getattr(leg_a_quote, "ask", None) if leg_a_quote else None,
            ),
            _node(
                "leg_b_quote",
                f"{_venue_label(leg_b_venue)}报价",
                _venue_quote_status(leg_b_venue, leg_b_quote, leg_b_age, session, settings.quote.stale_ms),
                _venue_quote_message(leg_b_venue, leg_b_quote, leg_b_age, session),
                age_ms=leg_b_age,
                source=getattr(leg_b_quote, "source", "") if leg_b_quote else "",
                bid=getattr(leg_b_quote, "bid", None) if leg_b_quote else None,
                ask=getattr(leg_b_quote, "ask", None) if leg_b_quote else None,
            ),
            _node("sync", "同步", _sync_status(sync_diff, leg_a_quote, leg_b_quote, settings), _sync_message(sync_diff, leg_a_quote, leg_b_quote, settings), latency_ms=sync_diff),
            _node("scan", "扫描", _scan_status(current), _scan_message(current, scan_age), age_ms=scan_age, latency_ms=scan_duration),
            _node("signal", "信号", _signal_status(current), _signal_message(current), latency_ms=signal_duration),
            _node("candidate", "候选", _candidate_status(opportunity), _candidate_message(opportunity), opportunity=opportunity),
            _node("stream", "前端推送", "pass" if current else "idle", "等待扫描状态" if not current else "SSE/接口可读取"),
        ],
        "edges": [
            _edge("leg_a_quote", "sync", leg_a_age, _venue_quote_status(leg_a_venue, leg_a_quote, leg_a_age, session, settings.quote.stale_ms), f"{leg_a_venue} age"),
            _edge("leg_b_quote", "sync", leg_b_age, _venue_quote_status(leg_b_venue, leg_b_quote, leg_b_age, session, settings.quote.stale_ms), f"{leg_b_venue} age"),
            _edge("sync", "scan", quote_sync_duration, _sync_status(sync_diff, leg_a_quote, leg_b_quote, settings), "quote sync"),
            _edge("scan", "signal", signal_duration, _scan_status(current), "signal calc"),
            _edge("signal", "candidate", candidate_sync_duration, _signal_status(current), "candidate sync"),
            _edge("candidate", "stream", None, "pass" if current else "idle", "push"),
        ],
        "metrics": {
            "hl_age_ms": leg_a_age,
            "mt5_age_ms": leg_b_age,
            "leg_a_age_ms": leg_a_age,
            "leg_b_age_ms": leg_b_age,
            "sync_diff_ms": sync_diff,
            "scan_age_ms": scan_age,
            "quote_sync_duration_ms": quote_sync_duration,
            "symbol_scan_duration_ms": scan_duration,
            "sizing_duration_ms": sizing_duration,
            "cost_duration_ms": cost_duration,
            "signal_duration_ms": signal_duration,
            "candidate_sync_duration_ms": candidate_sync_duration,
            "persist_duration_ms": persist_duration,
            "gross_spread": current.get("gross_spread") if current else None,
            "unit_net_profit": current.get("unit_net_profit") if current else None,
            "annualized_return": current.get("annualized_return") if current else None,
        },
    }


# ---------------------------------------------------------------------------
# 管线状态与阻塞检测
# ---------------------------------------------------------------------------

def _pipeline_status(current, hl_quote, mt5_quote, session, hl_age, mt5_age, sync_diff, settings, leg_a_venue: str = "hyperliquid", leg_b_venue: str = "mt5") -> tuple[str, str, str]:
    """判断管线整体状态：blocked / warning / flowing"""
    blockers = _pipeline_blockers(current, hl_quote, mt5_quote, session, hl_age, mt5_age, sync_diff, settings, leg_a_venue, leg_b_venue)
    if blockers:
        first = blockers[0]
        return first["stage"], "blocked", first["message"]
    if not current:
        return "scan", "warning", "等待扫描器产出当前状态"
    current_status = str(current.get("status", ""))
    reason = str(current.get("reason", "") or current.get("reject_reason", "") or "")
    if current_status == "candidate":
        return "candidate", "warning", reason or "有候选但未达执行线"
    if current_status == "executable":
        return "candidate", "flowing", reason or "可执行机会"
    return "stream", "flowing", reason or "链路正常"


def _pipeline_blockers(current, hl_quote, mt5_quote, session, hl_age, mt5_age, sync_diff, settings, leg_a_venue: str = "hyperliquid", leg_b_venue: str = "mt5") -> list[dict[str, str]]:
    """检测管线中所有阻塞原因，按顺序返回阻塞列表。"""
    blockers: list[dict[str, str]] = []
    # A 腿报价检查
    if not hl_quote:
        blockers.append({"stage": "leg_a_quote", "message": f"缺少 {leg_a_venue} 报价"})
    elif hl_age is not None and hl_age > settings.quote.stale_ms:
        blockers.append({"stage": "leg_a_quote", "message": f"{leg_a_venue} 行情过期 {hl_age:.0f}ms"})
    # B 腿报价检查
    if not mt5_quote:
        blockers.append({"stage": "leg_b_quote", "message": f"缺少 {leg_b_venue} 报价"})
    elif mt5_age is not None and mt5_age > settings.quote.stale_ms:
        blockers.append({"stage": "leg_b_quote", "message": f"{leg_b_venue} 行情过期 {mt5_age:.0f}ms"})
    # MT5 可报价性检查
    if "mt5" in {leg_a_venue, leg_b_venue} and not session.can_quote:
        mt5_stage = "leg_a_quote" if leg_a_venue == "mt5" else "leg_b_quote"
        blockers.append({"stage": mt5_stage, "message": f"mt5 不可报价: {session.status}"})
    # 报价同步检查
    if hl_quote and mt5_quote and sync_diff is not None and sync_diff > settings.quote.loose_sync_ms:
        blockers.append({"stage": "sync", "message": f"行情未对齐，时间差 {sync_diff:.0f}ms"})
    if not current:
        return blockers
    # 扫描状态中的拒绝原因补充
    current_status = str(current.get("status", ""))
    if current_status == "rejected":
        reason = str(current.get("reason", "") or current.get("reject_reason", "") or "扫描拒绝")
        if "MT5" in reason and ("不可报价" in reason or "不可交易" in reason):
            mt5_stage = "leg_a_quote" if leg_a_venue == "mt5" else "leg_b_quote"
            if not any(item["stage"] == mt5_stage and item["message"] == reason for item in blockers):
                blockers.append({"stage": mt5_stage, "message": reason})
    return blockers


# ---------------------------------------------------------------------------
# 节点 / 边状态判断
# ---------------------------------------------------------------------------

def _venue_label(venue: str) -> str:
    """返回交易所的展示名称"""
    labels = {"hyperliquid": "Hyperliquid", "mt5": "MT5", "binance": "Binance", "okx": "OKX", "bybit": "Bybit"}
    return labels.get(str(venue or "").lower(), str(venue or "").upper() or "-")


def _quote_status(quote, age_ms: float | None, stale_ms: int) -> str:
    """判断通用报价状态：blocked / warning / flowing"""
    if not quote:
        return "blocked"
    if age_ms is not None and age_ms > stale_ms:
        return "blocked"
    if age_ms is not None and age_ms > stale_ms * 0.7:
        return "warning"
    return "flowing"


def _venue_quote_status(venue: str, quote, age_ms: float | None, session, stale_ms: int) -> str:
    """判断指定交易所的报价状态（MT5 需额外检查会话状态）"""
    if venue == "mt5":
        return _mt5_quote_status(quote, age_ms, session, stale_ms)
    return _quote_status(quote, age_ms, stale_ms)


def _mt5_quote_status(quote, age_ms: float | None, session, stale_ms: int) -> str:
    """MT5 报价状态：不可报价时直接 blocked"""
    if not session.can_quote:
        return "blocked"
    return _quote_status(quote, age_ms, stale_ms)


def _sync_status(sync_diff: float | None, hl_quote, mt5_quote, settings) -> str:
    """判断报价同步状态"""
    if not hl_quote or not mt5_quote:
        return "idle"
    if sync_diff is not None and sync_diff > settings.quote.loose_sync_ms:
        return "blocked"
    if sync_diff is not None and sync_diff > settings.quote.loose_sync_ms * 0.7:
        return "warning"
    return "pass"


def _scan_status(current: dict[str, Any] | None) -> str:
    """判断扫描状态"""
    if not current:
        return "idle"
    return "pass"


def _signal_status(current: dict[str, Any] | None) -> str:
    """判断信号计算状态"""
    if not current:
        return "idle"
    status = str(current.get("status", ""))
    if status == "rejected":
        return "warning"
    if status == "candidate":
        return "warning"
    if status == "executable":
        return "flowing"
    return "pass"


def _candidate_status(opportunity: dict[str, Any] | None) -> str:
    """判断候选机会状态"""
    if not opportunity:
        return "idle"
    if opportunity.get("status") == "executable":
        return "flowing"
    if opportunity.get("status") == "executing":
        return "warning"
    return "warning"


# ---------------------------------------------------------------------------
# 节点 / 边消息文本
# ---------------------------------------------------------------------------

def _quote_message(quote, age_ms: float | None) -> str:
    """生成报价消息文本"""
    if not quote:
        return "无报价"
    return f"{quote.source}; age={_fmt_ms(age_ms)}"


def _venue_quote_message(venue: str, quote, age_ms: float | None, session) -> str:
    """生成指定交易所的报价消息"""
    if venue == "mt5":
        return _mt5_quote_message(quote, age_ms, session)
    return _quote_message(quote, age_ms)


def _mt5_quote_message(quote, age_ms: float | None, session) -> str:
    """生成 MT5 报价消息（不可报价时显示会话状态）"""
    if not session.can_quote:
        return f"{session.status}: {session.reason}"
    return _quote_message(quote, age_ms)


def _sync_message(sync_diff: float | None, hl_quote, mt5_quote, settings) -> str:
    """生成同步消息文本"""
    if not hl_quote or not mt5_quote:
        return "等待双边报价"
    if sync_diff is not None and sync_diff > settings.quote.loose_sync_ms:
        return f"时间差 {_fmt_ms(sync_diff)} > {settings.quote.loose_sync_ms}ms"
    return f"时间差 {_fmt_ms(sync_diff)}"


def _scan_message(current: dict[str, Any] | None, scan_age: float | None) -> str:
    """生成扫描消息文本"""
    if not current:
        return "未产生扫描状态"
    return f"{current.get('status', '-')}; age={_fmt_ms(scan_age)}"


def _signal_message(current: dict[str, Any] | None) -> str:
    """生成信号消息文本"""
    if not current:
        return "等待扫描"
    reason = str(current.get("reason", "") or "")
    return reason[:120] if reason else str(current.get("status", "-"))


def _candidate_message(opportunity: dict[str, Any] | None) -> str:
    """生成候选机会消息文本"""
    if not opportunity:
        return "未生成"
    return f"{opportunity.get('status', '-')}; 净利={float(opportunity.get('net_profit') or 0):.2f}"


def _best_opportunity(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从机会列表中选取最优机会（executable > executing > candidate）"""
    if not rows:
        return None
    rank = {"executable": 3, "executing": 2, "candidate": 1}
    return sorted(rows, key=lambda row: rank.get(str(row.get("status", "")), 0), reverse=True)[0]


# ---------------------------------------------------------------------------
# 对冲池
# ---------------------------------------------------------------------------

def _pool_payload(groups: list[HedgeGroup | HedgeGroupSnapshot], now: datetime, mappings_by_symbol: dict[str, SymbolMapping] | None = None) -> dict[str, Any]:
    """构建对冲池诊断数据，包含分组项和泳道统计"""
    items = sorted((_group_payload(group, now, mappings_by_symbol) for group in groups), key=_pool_item_sort_key)
    lanes = [
        {"key": "pending", "label": "待执行", "count": _lane_count(items, "pending")},
        {"key": "opening", "label": "建仓中", "count": _lane_count(items, "opening")},
        {"key": "open", "label": "持仓中", "count": _lane_count(items, "open")},
        {"key": "ready_to_close", "label": "可平仓", "count": _lane_count(items, "ready_to_close")},
        {"key": "closing", "label": "平仓中", "count": _lane_count(items, "closing")},
        {"key": "manual", "label": "异常", "count": _lane_count(items, "manual")},
    ]
    return {"items": items, "lanes": lanes, "active_total": len(items)}


def _pool_item_sort_key(item: dict[str, Any]) -> tuple[int, str, int]:
    """对冲池项排序键：阶段 → 品种 → ID"""
    return (
        POOL_STAGE_ORDER.get(str(item.get("stage") or ""), 99),
        str(item.get("symbol") or ""),
        int(item.get("id") or 0),
    )


def _group_payload(group: HedgeGroup | HedgeGroupSnapshot, now: datetime, mappings_by_symbol: dict[str, SymbolMapping] | None = None) -> dict[str, Any]:
    """构建单个对冲组的诊断数据"""
    stage = _group_stage(group, mappings_by_symbol)
    mapping = (mappings_by_symbol or {}).get(group.symbol)
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a") if mapping else ("hyperliquid", "")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b") if mapping else ("mt5", "")
    spreads = hedge_group_spreads(group, mapping=mapping)
    unrealized_pnl = _runtime_unrealized_pnl(group, spreads)
    updated_at = getattr(group, "updated_at", None) or getattr(group, "opened_at", None) or getattr(group, "closed_at", None)
    return {
        "id": group.id,
        "symbol": group.symbol,
        "direction": group.direction,
        "leg_a_venue": leg_a_venue,
        "leg_a_symbol": leg_a_symbol,
        "leg_b_venue": leg_b_venue,
        "leg_b_symbol": leg_b_symbol,
        "status": group.status,
        "stage": stage,
        "stage_label": _stage_label(stage),
        "execution_mode": group.execution_mode,
        "notional": group.notional,
        "quantity": group.quantity,
        "trigger_spread": group.trigger_spread,
        "entry_spread": group.entry_spread,
        "current_entry_spread": spreads["current_entry_spread"],
        "current_close_spread": spreads["current_close_spread"],
        "quote_time_diff_ms": spreads["quote_time_diff_ms"],
        "quote_age_ms": spreads["quote_age_ms"],
        "exit_target": group.exit_target,
        "realized_pnl": group.realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "accrued_fees": float(getattr(group, "fees", 0.0) or 0.0),
        "remaining_close_fee": (
            float(getattr(group, "estimated_close_fee", 0.0) or 0.0)
            if group.status in {"open", "open_partial"} else 0.0
        ),
        "pnl_basis": "liquidation" if group.status in {"open", "open_partial"} else "realized",
        "close_reason": group.close_reason,
        "age_ms": _age_ms(now, updated_at),
    }


def _runtime_unrealized_pnl(group: HedgeGroup | HedgeGroupSnapshot, spreads: dict[str, Any]) -> float:
    """计算运行时未实现盈亏（持仓中通过平仓价差实时计算）"""
    current_close_spread = spreads.get("current_close_spread")
    if group.status in {"open", "open_partial"} and current_close_spread is not None:
        try:
            return liquidation_pnl_from_close_spread(group, float(current_close_spread))
        except (TypeError, ValueError):
            return float(group.unrealized_pnl or 0.0)
    return float(group.unrealized_pnl or 0.0)


def _group_stage(group: HedgeGroup | HedgeGroupSnapshot, mappings_by_symbol: dict[str, SymbolMapping] | None = None) -> str:
    """判断对冲组当前所处阶段"""
    if group.status in {"pending_open"}:
        return "pending"
    if group.status in {"opening"}:
        return "opening"
    if group.status in {"closing"}:
        return "closing"
    if group.status in {"manual_intervention", "failed"}:
        return "manual"
    if group.status in {"open", "open_partial"}:
        mapping = (mappings_by_symbol or {}).get(group.symbol)
        current = hedge_group_spreads(group, mapping=mapping).get("current_close_spread")
        unrealized_pnl = _runtime_unrealized_pnl(group, {"current_close_spread": current})
        if group.exit_target and group.entry_spread and unrealized_pnl > 0:
            return "ready_to_close"
        return "open"
    return "open"


def _stage_label(stage: str) -> str:
    """返回阶段的中文标签"""
    return {
        "pending": "待执行",
        "opening": "建仓中",
        "open": "持仓中",
        "ready_to_close": "可平仓",
        "closing": "平仓中",
        "manual": "异常",
    }.get(stage, stage)


def _lane_count(items: list[dict[str, Any]], stage: str) -> int:
    """统计指定阶段的对冲组数量"""
    return sum(1 for item in items if item.get("stage") == stage)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def _summary(symbols: list[dict[str, Any]], pool: dict[str, Any]) -> dict[str, Any]:
    """汇总所有品种和对冲池的状态统计"""
    return {
        "enabled_symbols": len(symbols),
        "flowing": sum(1 for item in symbols if item["status"] == "flowing"),
        "blocked": sum(1 for item in symbols if item["status"] == "blocked"),
        "warning": sum(1 for item in symbols if item["status"] == "warning"),
        "candidate": sum(1 for item in symbols if item["nodes"][6].get("status") in {"flowing", "warning"}),
        "pool_active": pool["active_total"],
        "ready_to_close": _lane_count(pool["items"], "ready_to_close"),
    }


# ---------------------------------------------------------------------------
# 通用辅助函数
# ---------------------------------------------------------------------------

def _node(key: str, label: str, status: str, message: str, **extra) -> dict[str, Any]:
    """构建管线节点"""
    return {"key": key, "label": label, "status": status, "message": message, **extra}


def _edge(source: str, target: str, latency_ms: float | None, status: str, label: str) -> dict[str, Any]:
    """构建管线边"""
    return {"source": source, "target": target, "latency_ms": latency_ms, "status": status, "label": label}


def _age_ms(now: datetime, timestamp: datetime | str | None) -> float | None:
    """计算时间戳到 now 的毫秒差"""
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    return max((now - timestamp).total_seconds() * 1000, 0.0)


def _metric_ms(row: dict[str, Any] | None, key: str) -> float | None:
    """从字典中安全提取毫秒级耗时指标"""
    if not row:
        return None
    try:
        return float(row.get(key))
    except (TypeError, ValueError):
        return None


def _fmt_ms(value: float | None) -> str:
    """格式化毫秒值为可读字符串"""
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"


def _model_dict(row) -> dict[str, Any]:
    """将 SQLAlchemy 模型实例转换为字典"""
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
