"""
价差扫描器模块
==============

核心扫描逻辑，每个扫描周期遍历所有已启用品种，执行：

1. 报价同步（quote synchronizer）
2. 仓位计算（position sizing）
3. 成本估算（cost estimation）
4. 统计信号评估（statistical signal）
5. 多级门控判定（signal / liquidity / market gates）
6. 价差分桶累积与持久化（bucket accumulator）
7. 套利机会同步（opportunity sync）

关键路径日志:
- 每次扫描开始/结束+耗时
- 每个 symbol 的 gate 结果
- bucket 累积/刷新
- 异常捕获

使用 ``utc_now`` 替代 ``datetime.now(timezone.utc).replace(tzinfo=None)``，
使用 ``TTLCache`` 替代手写 strategy_cache，
使用 ``get_logger`` 统一日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import monotonic, perf_counter
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import (
    ArbitrageOpportunity, MarketSnapshot, SpreadBucket, SpreadCurrent,
    SpreadDirectionCurrent, SpreadSnapshot, StrategySetting, SymbolMapping,
    SystemLog, WorkerRun,
)
from app.db.retention import prune_table_by_id
from app.market.symbols import enabled_mappings
from app.market.orderbook import order_book_cache, simulate_market_fill
from app.market.quotes import quote_cache, quote_synchronizer
from app.market.scan_state import scan_state_store
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.market.mt5_tradability import mt5_tradability_cache
from app.strategy.cost import estimate_pair_cost
from app.strategy.live_costs import VenueCostUnavailable, venue_cost_inputs
from app.strategy.position_sizing import PositionSizing, calculate_position_sizing
from app.strategy.statistical_signal import evaluate_entry_signal
from app.execution.circuit_breaker import feed_spread as breaker_feed
from app.execution.modes import MAKER_THEN_MARKET, close_order_type, execution_mode, maker_leg, open_order_type
from app.strategy.spread_math import DIRECTIONS, LONG_LEG_A_SHORT_LEG_B, spreads_for_direction
from app.adapters.venue import is_native_pair, mapping_leg

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class BucketAccumulator:
    """价差分桶累加器 —— 在一个 bucket 窗口内累积 OHLC 等统计量。"""
    symbol: str
    direction: str
    bucket_start: datetime
    bucket_seconds: int
    open_spread: float
    high_spread: float
    low_spread: float
    close_spread: float
    unit_cost_sum: float
    unit_net_profit_sum: float
    spread_sum: float
    close_basis_sum: float
    mid_spread_sum: float
    spread_cost_sum: float
    sample_count: int


@dataclass(frozen=True)
class GateResult:
    """门控判定结果。"""
    status: str    # pass / candidate / executable / rejected
    reason: str
    gate: str      # signal / liquidity / market
    blocker: str = ""


# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------

_bucket_accumulators: dict[tuple[str, str], BucketAccumulator] = {}
_last_snapshot_flush: dict[tuple[str, str], float] = {}
_scan_timings: dict[str, dict[str, float]] = {}

# 策略配置缓存：使用 TTLCache 替代手写 tuple+monotonic
from app.core.cache import TTLCache
_strategy_cache: TTLCache[SimpleNamespace] = TTLCache(ttl_seconds=2.0, namespace="strategy-settings")


def clear_strategy_setting_cache() -> None:
    """清空策略配置缓存。"""
    _strategy_cache.clear()


def get_strategy_setting(db: Session) -> SimpleNamespace:
    """获取策略配置（带 2 秒 TTL 缓存）。

    生产数据库使用稳定 URL 键，供 API 与 Worker 共享；内存数据库按实例隔离。
    """
    bind = db.get_bind()
    bind_key = str(bind.url)
    if bind_key.endswith(":memory:"):
        bind_key = f"{bind_key}:{id(bind)}"
    cache_key = f"strategy_{bind_key}"
    cached = _strategy_cache.get(cache_key)
    if cached:
        return cached
    setting = db.query(StrategySetting).first()
    if not setting:
        setting = StrategySetting()
        db.add(setting)
        db.commit()
        db.refresh(setting)
    cached = SimpleNamespace(**{column.name: getattr(setting, column.name) for column in setting.__table__.columns})
    _strategy_cache.set(cache_key, cached)
    return cached


# ---------------------------------------------------------------------------
# 主扫描函数
# ---------------------------------------------------------------------------

def run_scan(db: Session) -> int:
    """执行一次完整的价差扫描。

    遍历所有已启用品种，计算价差、成本、信号，判定门控状态。

    参数:
        db: 数据库会话

    返回:
        新增/更新的套利机会数量
    """
    started = perf_counter()
    created = 0
    strategy = get_strategy_setting(db)
    settings = get_settings()
    logger.info("价差扫描开始")
    try:
        current_payloads: list[dict] = []
        direction_payloads_all: list[dict] = []
        opportunity_payloads: list[dict] = []
        for mapping in enabled_mappings(db):
            symbol_started = perf_counter()
            timings: dict[str, float] = {}
            try:
                # ── 非原生品种对：只读模式 ──────────────────────────────────
                if not is_native_pair(mapping):
                    persist_started = perf_counter()
                    readonly_payloads = _readonly_leg_pair_payloads(mapping, settings, strategy)
                    if readonly_payloads:
                        direction_payloads_all.extend(readonly_payloads)
                        current_payloads.append(_best_current_payload(readonly_payloads))
                    else:
                        current_payloads.append(_current_payload(
                            symbol=mapping.symbol,
                            direction="none",
                            leg_a_bid=0, leg_a_ask=0, leg_b_bid=0, leg_b_ask=0,
                            quantity=0, gross_spread=0, unit_cost=0, unit_net_profit=0,
                            total_cost=0, net_profit=0, annualized_return=0,
                            status="rejected", reason="缺少原生连接器行情",
                            gate="quote", blocker="quote",
                        ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue

                # ── MT5 会话状态检查 ──────────────────────────────────────
                quote_sync_started = perf_counter()
                has_mt5 = "mt5" in {str(mapping.leg_a_venue).lower(), str(mapping.leg_b_venue).lower()}
                session_state = mt5_session_state(mapping) if has_mt5 else SimpleNamespace(
                    can_quote=True,
                    status="not_applicable",
                    reason="",
                )
                if not session_state.can_quote:
                    _record_duration(timings, "quote_sync_duration_ms", quote_sync_started)
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol, direction="none",
                        leg_a_bid=0, leg_a_ask=0, leg_b_bid=0, leg_b_ask=0,
                        quantity=0, gross_spread=0, unit_cost=0, unit_net_profit=0,
                        total_cost=0, net_profit=0, annualized_return=0,
                        status="rejected",
                        reason=f"MT5 不可报价/不可交易: {session_state.status}，{session_state.reason}",
                        gate="market", blocker="market",
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue

                # ── 报价同步 ──────────────────────────────────────────────
                leg_a_venue_name, _ = mapping_leg(mapping, "a")
                leg_b_venue_name, _ = mapping_leg(mapping, "b")
                leg_meta = _leg_metadata(mapping)
                synced, sync_reason = quote_synchronizer.synchronized(
                    mapping.symbol,
                    mode="loose",
                    max_time_diff_ms=settings.quote.loose_sync_ms,
                    max_age_ms=max(settings.quote.stale_ms, settings.quote.loose_sync_ms),
                    leg_a_venue=leg_a_venue_name,
                    leg_b_venue=leg_b_venue_name,
                )
                _record_duration(timings, "quote_sync_duration_ms", quote_sync_started)
                if not synced:
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol, direction="none",
                        leg_a_bid=0, leg_a_ask=0, leg_b_bid=0, leg_b_ask=0,
                        quantity=0, gross_spread=0, unit_cost=0, unit_net_profit=0,
                        total_cost=0, net_profit=0, annualized_return=0,
                        status="rejected", reason=sync_reason,
                        gate="quote", blocker="quote",
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue

                hl = synced.leg_a
                mt = synced.leg_b

                # ── 仓位计算 ──────────────────────────────────────────────
                sizing_started = perf_counter()
                try:
                    target_notional = getattr(mapping, "target_notional", strategy.default_notional)
                    sizing = _position_sizing(mapping, mt.mid, hl.mid, target_notional)
                except ValueError as exc:
                    _record_duration(timings, "sizing_duration_ms", sizing_started)
                    persist_started = perf_counter()
                    current_payloads.append(_current_payload(
                        symbol=mapping.symbol, direction="none",
                        leg_a_bid=hl.bid, leg_a_ask=hl.ask, leg_b_bid=mt.bid, leg_b_ask=mt.ask,
                        quantity=0, leg_b_quantity=0, leg_a_quantity=0,
                        notional_currency=(mapping.mt5_currency_profit or mapping.quote_asset or "USD"),
                        fx_rate_to_usd=0, gross_spread=0, unit_cost=0, unit_net_profit=0,
                        total_cost=0, net_profit=0, annualized_return=0,
                        status="rejected", reason=str(exc),
                        gate="market", blocker="sizing",
                    ))
                    _record_duration(timings, "persist_duration_ms", persist_started)
                    continue
                _record_duration(timings, "sizing_duration_ms", sizing_started)

                # ── 成本估算 + 信号评估 + 门控判定 ────────────────────────
                holding_minutes = getattr(mapping, "max_holding_minutes", strategy.max_holding_minutes)
                holding_hours = max(holding_minutes / 60, 1)
                leg_a_symbol = mapping_leg(mapping, "a")[1]
                leg_b_symbol = mapping_leg(mapping, "b")[1]
                leg_a_costs = venue_cost_inputs(leg_a_venue_name, leg_a_symbol)
                leg_b_costs = venue_cost_inputs(leg_b_venue_name, leg_b_symbol)
                persist_started = perf_counter()
                direction_payloads = []
                cost_started = perf_counter()
                signal_started = perf_counter()
                candidate_started = perf_counter()
                for direction in DIRECTIONS:
                    spread_values = spreads_for_direction(direction, hl.bid, hl.ask, mt.bid, mt.ask)
                    gross_spread = spread_values.entry_spread
                    breaker_feed(mapping.symbol, direction, spread_values.entry_spread)
                    gross_profit = gross_spread * sizing.leg_a_quantity
                    quantity = sizing.leg_b_quantity
                    notional = sizing.notional_usd
                    leg_a_side = "buy" if direction == LONG_LEG_A_SHORT_LEG_B else "sell"
                    leg_b_side = "sell" if direction == LONG_LEG_A_SHORT_LEG_B else "buy"
                    cost = estimate_pair_cost(
                        notional=notional,
                        leg_a_open_fee_rate=_venue_fee_rate(
                            open_order_type(mapping, "a"), leg_a_costs,
                            post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "a",
                        ),
                        leg_a_close_fee_rate=_venue_fee_rate(close_order_type(mapping, "a"), leg_a_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "a"),
                        leg_b_open_fee_rate=_venue_fee_rate(open_order_type(mapping, "b"), leg_b_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "b"),
                        leg_b_close_fee_rate=_venue_fee_rate(close_order_type(mapping, "b"), leg_b_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "b"),
                        source=f"{leg_a_costs.source};{leg_b_costs.source}",
                    )
                    unit_cost = cost.total / sizing.leg_a_quantity if sizing.leg_a_quantity > 0 else cost.total
                    # 第一次评估用于得到统计退出线；最终收益必须扣除退出价差，
                    # 因为入场和平仓均使用可成交 bid/ask，不能另行重复扣点差。
                    provisional_net_profit = gross_profit - cost.total
                    provisional_unit_net_profit = gross_spread - unit_cost
                    provisional_annualized_return = (provisional_net_profit / notional) * (365 * 24 / holding_hours)
                    statistical_signal = evaluate_entry_signal(
                        db, strategy, mapping.symbol, direction,
                        gross_spread, unit_cost, provisional_unit_net_profit,
                        provisional_net_profit, provisional_annualized_return,
                    )
                    entry_threshold = _effective_entry_threshold(mapping, statistical_signal.reachable_entry)
                    exit_target = _effective_exit_target(mapping, statistical_signal.exit_target)
                    net_profit, unit_net_profit = _projected_profit(
                        gross_spread, exit_target, cost.total, sizing.leg_a_quantity,
                    )
                    annualized_return = (net_profit / notional) * (365 * 24 / holding_hours)
                    # 用包含退出线摩擦的最终净利润再次执行利润门槛。
                    statistical_signal = evaluate_entry_signal(
                        db, strategy, mapping.symbol, direction,
                        gross_spread, unit_cost, unit_net_profit, net_profit, annualized_return,
                    )
                    entry_threshold = _effective_entry_threshold(mapping, statistical_signal.reachable_entry)
                    exit_target = _effective_exit_target(mapping, statistical_signal.exit_target)
                    signal_gate = _signal_gate(mapping, statistical_signal.result, gross_spread)
                    risk_tags = _risk_tags(gross_spread, statistical_signal)
                    liquidity_gate = _liquidity_gate(
                        mapping.symbol, leg_a_side, sizing.leg_a_quantity,
                        notional, hl.depth_notional, signal_gate.status, leg_a_venue_name,
                    )
                    market_gate = (
                        _direction_market_gate(session_state, mapping.symbol, direction, leg_b_side)
                        if has_mt5
                        else GateResult("candidate", "", "market", "")
                    )
                    final_gate = _combine_gates(signal_gate, liquidity_gate, market_gate)
                    reason = final_gate.reason or f"loose_sync={synced.time_diff_ms:.0f}ms; mt5_session={session_state.status}"
                    payload = dict(
                        symbol=mapping.symbol, direction=direction, **leg_meta,
                        leg_a_bid=hl.bid, leg_a_ask=hl.ask, leg_b_bid=mt.bid, leg_b_ask=mt.ask,
                        quantity=quantity, leg_b_quantity=sizing.leg_b_quantity, leg_a_quantity=sizing.leg_a_quantity,
                        notional_currency=sizing.currency, fx_rate_to_usd=sizing.fx_rate_to_usd,
                        gross_spread=gross_spread,
                        entry_spread=spread_values.entry_spread,
                        close_spread=spread_values.close_spread,
                        mid_spread=spread_values.mid_spread,
                        spread_cost=spread_values.spread_cost,
                        unit_cost=unit_cost, unit_net_profit=unit_net_profit,
                        total_cost=cost.total, net_profit=net_profit,
                        annualized_return=annualized_return,
                        status=final_gate.status, reason=reason,
                        gate=final_gate.gate, blocker=final_gate.blocker,
                        risk_tags=risk_tags,
                        sampled_at=utc_now(),
                        leg_a_captured_at=hl.local_recv_ts,
                        leg_b_captured_at=mt.local_recv_ts,
                        leg_a_depth_notional=hl.depth_notional,
                        leg_b_depth_notional=mt.depth_notional,
                    )
                    direction_payloads_all.append(payload)
                    opportunity_payload = _opportunity_payload(
                        payload, notional=notional, entry_threshold=entry_threshold,
                        exit_target=exit_target,
                        overheat_threshold=statistical_signal.overheat,
                        signal_sample_count=statistical_signal.sample_count,
                        reason=reason,
                    )
                    if opportunity_payload:
                        created += 1
                        opportunity_payloads.append(opportunity_payload)
                    direction_payloads.append(payload)
                _record_duration(timings, "cost_duration_ms", cost_started)
                _record_duration(timings, "signal_duration_ms", signal_started)
                _record_duration(timings, "candidate_sync_duration_ms", candidate_started)
                best_payload = _best_current_payload(direction_payloads)
                current_payloads.append(best_payload)
                _record_duration(timings, "persist_duration_ms", persist_started)
                logger.debug(
                    "扫描 {}: status={}, gate={}, spread={:.2f}",
                    mapping.symbol, best_payload.get("status"), best_payload.get("gate"), best_payload.get("gross_spread", 0),
                )
            finally:
                timings["symbol_scan_duration_ms"] = _elapsed_ms(symbol_started)
                _scan_timings[mapping.symbol.upper()] = timings
        _update_scan_state_store(current_payloads, opportunity_payloads, direction_payloads_all)
        elapsed_ms = (perf_counter() - started) * 1000
        logger.info("价差扫描完成: 耗时 {:.0f}ms, 机会数 {}", elapsed_ms, created)
        return created
    except Exception as exc:
        db.rollback()
        logger.error("价差扫描失败: {}", exc)
        try:
            db.add(WorkerRun(worker_name="spread_scanner", status="failed", duration_ms=int((perf_counter() - started) * 1000), error_message=str(exc)))
            db.add(SystemLog(level="error", category="scanner", message="价差扫描失败", context=str(exc)))
            prune_table_by_id(db, WorkerRun)
            prune_table_by_id(db, SystemLog)
            db.commit()
        except Exception:
            db.rollback()
        raise


# ---------------------------------------------------------------------------
# 仓位计算
# ---------------------------------------------------------------------------

def _position_sizing(mapping, leg_b_mid: float, leg_a_mid: float, target_notional_usd: float) -> PositionSizing:
    """兼容旧调用点，实际换算统一委托给 position_sizing 模块。"""
    return calculate_position_sizing(
        mapping,
        leg_b_mid=leg_b_mid,
        leg_a_mid=leg_a_mid,
        target_notional_usd=target_notional_usd,
    )


def _venue_fee_rate(order_type: str, costs, *, post_only: bool = False) -> float:
    """只有 Post-only 限价单确定为 Maker，其余按 Taker 保守估算。"""
    is_maker = str(order_type or "").lower() == "limit" and post_only
    return costs.maker_fee_rate if is_maker else costs.taker_fee_rate


def _projected_profit(
    entry_spread: float,
    exit_spread: float,
    total_cost: float,
    quantity: float,
) -> tuple[float, float]:
    """按可成交入场/退出价差计算预计总利润和单位利润。"""
    qty = max(float(quantity or 0.0), 0.0)
    unit_cost = float(total_cost or 0.0) / qty if qty > 0 else float(total_cost or 0.0)
    unit_profit = float(entry_spread or 0.0) - float(exit_spread or 0.0) - unit_cost
    return unit_profit * qty, unit_profit


# ---------------------------------------------------------------------------
# 只读品种对载荷
# ---------------------------------------------------------------------------

def _readonly_leg_pair_payloads(mapping, settings, strategy=None) -> list[dict]:
    """为暂未进入自动执行矩阵的品种对生成只读价差载荷。"""
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
    leg_meta = _leg_metadata(mapping)
    leg_a = quote_cache.latest(leg_a_venue, mapping.symbol)
    leg_b = quote_cache.latest(leg_b_venue, mapping.symbol)
    if not leg_a or not leg_b:
        return []
    now = utc_now()
    max_age = max((now - leg_a.local_recv_ts).total_seconds() * 1000, (now - leg_b.local_recv_ts).total_seconds() * 1000)
    if max_age > max(settings.quote.stale_ms, settings.quote.loose_sync_ms):
        return []
    time_diff = abs((leg_a.local_recv_ts - leg_b.local_recv_ts).total_seconds() * 1000)
    if time_diff > settings.quote.loose_sync_ms:
        return []
    if leg_a.bid <= 0 or leg_a.ask <= 0 or leg_b.bid <= 0 or leg_b.ask <= 0:
        return []
    target_notional = max(float(getattr(mapping, "target_notional", getattr(strategy, "default_notional", 0.0)) or 0.0), 1.0)
    try:
        sizing = calculate_position_sizing(
            mapping,
            leg_b_mid=leg_b.mid,
            leg_a_mid=leg_a.mid,
            target_notional_usd=target_notional,
        )
    except ValueError:
        return []
    notional = sizing.notional_usd
    holding_hours = max(float(getattr(mapping, "max_holding_minutes", getattr(strategy, "max_holding_minutes", 60.0)) or 60.0) / 60, 1.0)
    try:
        leg_a_costs = venue_cost_inputs(leg_a_venue, leg_a_symbol)
        leg_b_costs = venue_cost_inputs(leg_b_venue, leg_b_symbol)
        cost_error = ""
    except VenueCostUnavailable as exc:
        leg_a_costs = leg_b_costs = None
        cost_error = str(exc)
    rows = []
    for direction in DIRECTIONS:
        spreads = spreads_for_direction(direction, leg_a.bid, leg_a.ask, leg_b.bid, leg_b.ask)
        leg_a_side = "buy" if direction == LONG_LEG_A_SHORT_LEG_B else "sell"
        leg_b_side = "sell" if direction == LONG_LEG_A_SHORT_LEG_B else "buy"
        if cost_error:
            total_cost = 0.0
            unit_cost = 0.0
            unit_net_profit = 0.0
            net_profit = 0.0
            status = "rejected"
            reason = f"自动成本不可用: {cost_error}"
            blocker = "cost"
        else:
            cost = estimate_pair_cost(
                notional=notional,
                leg_a_open_fee_rate=_venue_fee_rate(
                    open_order_type(mapping, "a"), leg_a_costs,
                    post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "a",
                ),
                leg_a_close_fee_rate=_venue_fee_rate(close_order_type(mapping, "a"), leg_a_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "a"),
                leg_b_open_fee_rate=_venue_fee_rate(open_order_type(mapping, "b"), leg_b_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "b"),
                leg_b_close_fee_rate=_venue_fee_rate(close_order_type(mapping, "b"), leg_b_costs, post_only=execution_mode(mapping) == MAKER_THEN_MARKET and maker_leg(mapping) == "b"),
                source=f"{leg_a_costs.source};{leg_b_costs.source}",
            )
            logger.debug(
                "成本明细: symbol={}, direction={}, notional={:.4f}, leg_a_qty={:.8f}, "
                "leg_b_lots={:.8f}, breakdown={}",
                mapping.symbol,
                direction,
                notional,
                sizing.leg_a_base_quantity,
                sizing.leg_b_lots,
                cost.as_dict(),
            )
            total_cost = cost.total
            unit_cost = total_cost / sizing.leg_a_base_quantity if sizing.leg_a_base_quantity > 0 else total_cost
            exit_target = _effective_exit_target(mapping, 0.0)
            net_profit, unit_net_profit = _projected_profit(
                spreads.entry_spread, exit_target, total_cost, sizing.leg_a_base_quantity,
            )
            status = "candidate"
            reason = (
                f"原生连接器只读候选: {leg_a_venue}/{leg_b_venue}; "
                f"自动成本源 {cost.source}; 不进入自动执行"
            )
            blocker = "execution"
        rows.append(_current_payload(
            symbol=mapping.symbol, direction=direction, **leg_meta,
            leg_a_bid=leg_a.bid, leg_a_ask=leg_a.ask, leg_b_bid=leg_b.bid, leg_b_ask=leg_b.ask,
            quantity=sizing.leg_b_lots,
            leg_b_quantity=sizing.leg_b_lots,
            leg_a_quantity=sizing.leg_a_base_quantity,
            notional_currency=sizing.currency,
            fx_rate_to_usd=sizing.fx_rate_to_usd, gross_spread=spreads.entry_spread,
            entry_spread=spreads.entry_spread, close_spread=spreads.close_spread, mid_spread=spreads.mid_spread,
            spread_cost=spreads.spread_cost, unit_cost=unit_cost,
            unit_net_profit=unit_net_profit,
            total_cost=total_cost, net_profit=net_profit,
            annualized_return=(net_profit / notional) * (365 * 24 / holding_hours) if notional > 0 else 0.0,
            status=status,
            reason=reason,
            gate="readonly" if not cost_error else "cost", blocker=blocker,
            sampled_at=now,
            leg_a_captured_at=leg_a.local_recv_ts, leg_b_captured_at=leg_b.local_recv_ts,
            leg_a_depth_notional=leg_a.depth_notional, leg_b_depth_notional=leg_b.depth_notional,
        ))
    return rows


def _leg_metadata(mapping) -> dict[str, str]:
    """提取品种映射的腿元数据。"""
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a")
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b")
    return {"leg_a_venue": leg_a_venue, "leg_a_symbol": leg_a_symbol, "leg_b_venue": leg_b_venue, "leg_b_symbol": leg_b_symbol}


# ---------------------------------------------------------------------------
# 门控判定
# ---------------------------------------------------------------------------

def _signal_gate(mapping: SymbolMapping, signal, gross_spread: float) -> GateResult:
    """信号门控：检查信号状态和最小入场价差。"""
    status = str(signal.status)
    reason = str(signal.reason or "")
    if status not in {"candidate", "executable"}:
        return GateResult(status, reason, "signal", "signal")
    min_entry_spread = float(mapping.min_entry_spread or 0.0)
    if min_entry_spread > 0 and gross_spread < min_entry_spread:
        return GateResult("candidate", f"未达到品种最小买入价差: {gross_spread:.2f} < {min_entry_spread:.2f}", "signal", "signal")
    return GateResult(status, reason, "signal", "" if status == "executable" else "signal")


def _risk_tags(gross_spread: float, statistical_signal) -> list[dict[str, float | str]]:
    """生成风险标签（如过热警告）。"""
    tags: list[dict[str, float | str]] = []
    overheat = float(getattr(statistical_signal, "overheat", 0.0) or 0.0)
    if overheat > 0 and gross_spread > overheat:
        tags.append({"type": "overheat", "message": f"价差超过过热线 {overheat:.2f}", "value": gross_spread, "threshold": overheat})
    return tags


def _liquidity_gate(symbol: str, side: str, quantity: float, notional: float, top_depth_notional: float, current_status: str, leg_a_venue: str = "hyperliquid") -> GateResult:
    """流动性门控：检查 L2 深度是否足够。"""
    if current_status not in {"candidate", "executable"}:
        return GateResult("pass", "", "liquidity")
    liquidity_reason = _leg_a_liquidity_reason(symbol, side, quantity, notional, top_depth_notional, leg_a_venue)
    if liquidity_reason:
        return GateResult("candidate", liquidity_reason, "liquidity", "liquidity")
    return GateResult("pass", "", "liquidity")


def _direction_market_gate(session_state, symbol: str, direction: str, mt5_side: str) -> GateResult:
    """市场门控：检查 MT5 会话和交易能力。"""
    mt5_open_allowed, mt5_open_reason = mt5_action_allowed(session_state, direction, "open")
    if not mt5_open_allowed:
        return GateResult("rejected", mt5_open_reason, "market", "market")
    tradability_allowed, tradability_reason = mt5_tradability_cache.is_fresh_allowed(symbol, mt5_side)
    if not tradability_allowed:
        return GateResult("rejected", f"MT5 交易能力未确认: {tradability_reason}", "market", "market")
    return GateResult("pass", "", "market")


def _combine_gates(signal_gate: GateResult, liquidity_gate: GateResult, market_gate: GateResult) -> GateResult:
    """合并多级门控结果。优先级：market(rejected) > signal(rejected) > liquidity(candidate) > signal(candidate)。"""
    for gate in (market_gate,):
        if gate.status == "rejected":
            return gate
    if signal_gate.status == "rejected":
        return signal_gate
    if liquidity_gate.status == "candidate":
        return liquidity_gate
    if signal_gate.status == "candidate":
        return signal_gate
    return GateResult("executable", signal_gate.reason, "signal")


def _leg_a_liquidity_reason(symbol: str, side: str, quantity: float, notional: float, top_depth_notional: float, leg_a_venue: str = "hyperliquid") -> str:
    """检查 Leg A 流动性是否充足，返回原因或空字符串。"""
    book = order_book_cache.latest(leg_a_venue, symbol)
    if book:
        fill = simulate_market_fill(book, side, quantity)
        if not fill.enough_liquidity:
            return f"{leg_a_venue} L2 深度不足: 目标 {quantity:.8f}，可成交 {fill.filled_quantity:.8f}"
        return ""
    if top_depth_notional > 0 and notional > top_depth_notional:
        return f"{leg_a_venue} 顶层深度不足: 目标 {notional:.2f} USD > 深度 {top_depth_notional:.2f} USD"
    return ""


def _effective_entry_threshold(mapping: SymbolMapping, statistical_threshold: float) -> float:
    """计算有效入场阈值（取统计阈值和品种最小入场的较大值）。"""
    min_entry_spread = float(getattr(mapping, "min_entry_spread", 0.0) or 0.0)
    return max(float(statistical_threshold or 0.0), min_entry_spread)


def _effective_exit_target(mapping: SymbolMapping, statistical_target: float) -> float:
    """计算有效出场目标（取统计目标和品种最大收盘价差的较小值）。"""
    max_close_spread = float(getattr(mapping, "max_close_spread", 0.0) or 0.0)
    statistical = float(statistical_target or 0.0)
    if max_close_spread == 0:
        return statistical
    if statistical == 0:
        return max_close_spread
    return min(statistical, max_close_spread)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000


def _record_duration(timings: dict[str, float], key: str, started: float) -> None:
    timings[key] = _elapsed_ms(started)


def _current_payload(**values) -> dict:
    """构造当前价差载荷，填充默认值。"""
    values.setdefault("entry_spread", values.get("gross_spread", 0.0))
    values.setdefault("close_spread", values.get("gross_spread", 0.0))
    values.setdefault("mid_spread", values.get("gross_spread", 0.0))
    values.setdefault("spread_cost", values.get("close_spread", 0.0) - values.get("entry_spread", 0.0))
    values.setdefault("leg_b_quantity", values.get("quantity", 0.0))
    values.setdefault("leg_a_quantity", values.get("quantity", 0.0))
    values.setdefault("notional_currency", "USD")
    values.setdefault("fx_rate_to_usd", 1.0)
    values.setdefault("sampled_at", utc_now())
    return values


def _opportunity_payload(payload, *, notional, entry_threshold, exit_target, overheat_threshold, signal_sample_count, reason) -> dict | None:
    """构造套利机会载荷（仅候选/可执行状态才生成）。"""
    if payload["status"] not in {"candidate", "executable", "executing"}:
        return None
    return {
        **{key: payload[key] for key in (
            "symbol", "direction", "leg_a_bid", "leg_a_ask", "leg_b_bid", "leg_b_ask",
            "quantity", "leg_b_quantity", "leg_a_quantity", "notional_currency", "fx_rate_to_usd",
            "gross_spread", "unit_cost", "unit_net_profit", "total_cost", "net_profit",
            "annualized_return", "status"
        )},
        "notional": notional, "entry_threshold": entry_threshold, "exit_target": exit_target,
        "overheat_threshold": overheat_threshold, "signal_sample_count": signal_sample_count,
        "reason": reason, "gate": payload.get("gate", ""), "blocker": payload.get("blocker", ""),
        "risk_tags": payload.get("risk_tags", []),
        "created_at": payload.get("sampled_at"), "updated_at": payload.get("sampled_at"),
    }


def _best_current_payload(payloads: list[dict]) -> dict:
    """从多个方向载荷中选取最佳（状态优先级最高 + 净利润最大）。"""
    if not payloads:
        raise ValueError("缺少双向价差结果")
    status_rank = {"executable": 3, "candidate": 2, "rejected": 1}
    return max(payloads, key=lambda row: (status_rank.get(str(row.get("status")), 0), float(row.get("net_profit") or 0.0)))


def _update_scan_state_store(spread_rows: list[dict], opportunity_rows: list[dict], direction_rows: list[dict]) -> None:
    """更新扫描状态存储。"""
    if not spread_rows and not opportunity_rows and not direction_rows:
        scan_state_store.update([], [])
        return
    spreads = sorted((_spread_state_dict(row) for row in spread_rows), key=lambda row: str(row.get("symbol", "")))
    opportunities = sorted(
        (_model_dict(row) for row in opportunity_rows),
        key=lambda row: row.get("updated_at") or row.get("created_at") or datetime.min,
        reverse=True,
    )[:50]
    directions = sorted(
        (_spread_state_dict(row) for row in direction_rows),
        key=lambda row: (str(row.get("symbol", "")), str(row.get("direction", ""))),
    )
    scan_state_store.update(spreads, opportunities, directions)


def _spread_state_dict(row) -> dict:
    data = _model_dict(row)
    data.update(_scan_timings.get(str(data.get("symbol", "")).upper(), {}))
    return data


def _model_dict(row) -> dict:
    if isinstance(row, dict):
        return dict(row)
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


# ---------------------------------------------------------------------------
# 持久化函数
# ---------------------------------------------------------------------------

def persist_scan_state(db: Session) -> dict[str, int]:
    """将扫描状态持久化到数据库。"""
    state = scan_state_store.snapshot()
    if not state["ready"]:
        return {"spread_direction_current": 0, "spread_current": 0, "opportunities": 0, "history": 0}
    settings = get_settings()
    direction_spreads = state.get("direction_spreads", [])
    current_spreads = state.get("spreads", [])
    opportunities = state.get("opportunities", [])
    ids_by_key: dict[tuple[str, str], int] = {}
    for payload in direction_spreads:
        _upsert_direction_current(db, **_filter_payload(payload, _spread_direction_fields()))
        _record_spread_history_from_payload(db, payload, settings)
    for payload in current_spreads:
        _upsert_current_spread(db, **_filter_payload(payload, _spread_current_fields()))
    scanned_symbols = {str(row.get("symbol", "")).upper() for row in [*direction_spreads, *current_spreads] if row.get("symbol")}
    changed_opportunities = _persist_opportunities(db, opportunities, ids_by_key, scanned_symbols)
    db.commit()
    scan_state_store.merge_opportunity_ids(ids_by_key)
    return {
        "spread_direction_current": len(direction_spreads),
        "spread_current": len(current_spreads),
        "opportunities": changed_opportunities,
        "history": len(direction_spreads),
    }


def _filter_payload(payload: dict, fields: set[str]) -> dict:
    return {key: payload[key] for key in fields if key in payload}


def _spread_current_fields() -> set[str]:
    return {
        "symbol", "direction", "leg_a_bid", "leg_a_ask", "leg_b_bid", "leg_b_ask",
        "quantity", "gross_spread", "unit_cost", "unit_net_profit", "total_cost", "net_profit",
        "annualized_return", "status", "reason", "entry_spread", "close_spread", "mid_spread",
        "spread_cost", "leg_b_quantity", "leg_a_quantity", "notional_currency", "fx_rate_to_usd",
    }


def _spread_direction_fields() -> set[str]:
    return _spread_current_fields() - {"direction"} | {"direction"}


def _upsert_current_spread(db: Session, *, symbol: str, direction: str, leg_a_bid: float, leg_a_ask: float, leg_b_bid: float, leg_b_ask: float, quantity: float, gross_spread: float, unit_cost: float, unit_net_profit: float, total_cost: float, net_profit: float, annualized_return: float, status: str, reason: str, entry_spread: float = 0.0, close_spread: float = 0.0, mid_spread: float = 0.0, spread_cost: float = 0.0, leg_b_quantity: float = 0.0, leg_a_quantity: float = 0.0, notional_currency: str = "USD", fx_rate_to_usd: float = 1.0) -> SpreadCurrent:
    """插入或更新 SpreadCurrent 记录。"""
    row = db.query(SpreadCurrent).filter(SpreadCurrent.symbol == symbol).first()
    if not row:
        row = SpreadCurrent(symbol=symbol)
        db.add(row)
    row.direction = direction
    row.leg_a_bid = leg_a_bid
    row.leg_a_ask = leg_a_ask
    row.leg_b_bid = leg_b_bid
    row.leg_b_ask = leg_b_ask
    row.quantity = quantity
    row.leg_b_quantity = leg_b_quantity or quantity
    row.leg_a_quantity = leg_a_quantity or quantity
    row.notional_currency = notional_currency
    row.fx_rate_to_usd = fx_rate_to_usd
    row.gross_spread = gross_spread
    row.entry_spread = entry_spread or gross_spread
    row.close_spread = close_spread if close_spread or entry_spread else gross_spread
    row.mid_spread = mid_spread if mid_spread or entry_spread else gross_spread
    row.spread_cost = spread_cost if spread_cost or entry_spread else row.close_spread - row.entry_spread
    row.unit_cost = unit_cost
    row.unit_net_profit = unit_net_profit
    row.total_cost = total_cost
    row.net_profit = net_profit
    row.annualized_return = annualized_return
    row.status = status
    row.reason = reason
    row.sampled_at = utc_now()
    return row


def _upsert_direction_current(db: Session, *, symbol: str, direction: str, leg_a_bid: float, leg_a_ask: float, leg_b_bid: float, leg_b_ask: float, quantity: float, gross_spread: float, entry_spread: float, close_spread: float, mid_spread: float, spread_cost: float, unit_cost: float, unit_net_profit: float, total_cost: float, net_profit: float, annualized_return: float, status: str, reason: str, leg_b_quantity: float = 0.0, leg_a_quantity: float = 0.0, notional_currency: str = "USD", fx_rate_to_usd: float = 1.0) -> SpreadDirectionCurrent:
    """插入或更新 SpreadDirectionCurrent 记录。"""
    row = db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol == symbol, SpreadDirectionCurrent.direction == direction).first()
    if not row:
        row = SpreadDirectionCurrent(symbol=symbol, direction=direction)
        db.add(row)
    row.leg_a_bid = leg_a_bid
    row.leg_a_ask = leg_a_ask
    row.leg_b_bid = leg_b_bid
    row.leg_b_ask = leg_b_ask
    row.quantity = quantity
    row.leg_b_quantity = leg_b_quantity or quantity
    row.leg_a_quantity = leg_a_quantity or quantity
    row.notional_currency = notional_currency
    row.fx_rate_to_usd = fx_rate_to_usd
    row.gross_spread = entry_spread
    row.entry_spread = entry_spread
    row.close_spread = close_spread
    row.mid_spread = mid_spread
    row.spread_cost = spread_cost
    row.unit_cost = unit_cost
    row.unit_net_profit = unit_net_profit
    row.total_cost = total_cost
    row.net_profit = net_profit
    row.annualized_return = annualized_return
    row.status = status
    row.reason = reason
    row.sampled_at = utc_now()
    return row


def _record_spread_history_from_payload(db: Session, payload: dict, settings) -> None:
    """从载荷记录价差历史（分桶累积 + 快照）。"""
    now = payload.get("sampled_at") or utc_now()
    bucket_seconds = max(settings.scanner.spread_bucket_seconds, 1)
    bucket_start_ts = int(now.timestamp()) // bucket_seconds * bucket_seconds
    bucket_start = datetime.utcfromtimestamp(bucket_start_ts)
    symbol = payload["symbol"]
    direction = payload["direction"]
    gross_spread = float(payload.get("gross_spread") or 0.0)
    unit_cost = float(payload.get("unit_cost") or 0.0)
    unit_net_profit = float(payload.get("unit_net_profit") or 0.0)
    close_spread = float(payload.get("close_spread") or gross_spread)
    mid_spread = float(payload.get("mid_spread") or gross_spread)
    spread_cost = float(payload.get("spread_cost") or 0.0)
    key = (symbol, direction)
    accumulator = _bucket_accumulators.get(key)
    if not accumulator or accumulator.bucket_start != bucket_start:
        if accumulator:
            _flush_bucket(db, accumulator)
        accumulator = BucketAccumulator(
            symbol=symbol, direction=direction, bucket_start=bucket_start,
            bucket_seconds=bucket_seconds, open_spread=gross_spread,
            high_spread=gross_spread, low_spread=gross_spread, close_spread=gross_spread,
            unit_cost_sum=unit_cost, unit_net_profit_sum=unit_net_profit,
            spread_sum=gross_spread, close_basis_sum=close_spread,
            mid_spread_sum=mid_spread, spread_cost_sum=spread_cost, sample_count=1,
        )
        _bucket_accumulators[key] = accumulator
    else:
        accumulator.high_spread = max(accumulator.high_spread, gross_spread)
        accumulator.low_spread = min(accumulator.low_spread, gross_spread)
        accumulator.close_spread = gross_spread
        accumulator.unit_cost_sum += unit_cost
        accumulator.unit_net_profit_sum += unit_net_profit
        accumulator.spread_sum += gross_spread
        accumulator.close_basis_sum += close_spread
        accumulator.mid_spread_sum += mid_spread
        accumulator.spread_cost_sum += spread_cost
        accumulator.sample_count += 1
    history_interval = max(settings.scanner.spread_history_interval_seconds, 1)
    last_flush = _last_snapshot_flush.get(key, 0.0)
    if now.timestamp() - last_flush < history_interval:
        return
    _flush_bucket(db, accumulator)
    _last_snapshot_flush[key] = now.timestamp()
    db.add(MarketSnapshot(platform="leg_a", symbol=symbol, bid=payload["leg_a_bid"], ask=payload["leg_a_ask"], mid=(payload["leg_a_bid"] + payload["leg_a_ask"]) / 2, depth_notional=float(payload.get("leg_a_depth_notional") or 0.0), captured_at=payload.get("leg_a_captured_at") or now))
    db.add(MarketSnapshot(platform="leg_b", symbol=symbol, bid=payload["leg_b_bid"], ask=payload["leg_b_ask"], mid=(payload["leg_b_bid"] + payload["leg_b_ask"]) / 2, depth_notional=float(payload.get("leg_b_depth_notional") or 0.0), captured_at=payload.get("leg_b_captured_at") or now))
    db.add(SpreadSnapshot(
        symbol=symbol, direction=direction,
        leg_a_bid=payload["leg_a_bid"], leg_a_ask=payload["leg_a_ask"],
        leg_b_bid=payload["leg_b_bid"], leg_b_ask=payload["leg_b_ask"],
        quantity=payload["quantity"],
        leg_b_quantity=payload.get("leg_b_quantity", payload["quantity"]),
        leg_a_quantity=payload.get("leg_a_quantity", payload["quantity"]),
        notional_currency=payload.get("notional_currency", "USD"),
        fx_rate_to_usd=payload.get("fx_rate_to_usd", 1.0),
        gross_spread=payload.get("entry_spread", gross_spread),
        entry_spread=payload.get("entry_spread", gross_spread),
        close_spread=close_spread, mid_spread=mid_spread, spread_cost=spread_cost,
        unit_cost=unit_cost, unit_net_profit=unit_net_profit,
        total_cost=payload.get("total_cost", 0.0),
        net_profit=payload.get("net_profit", 0.0),
        annualized_return=payload.get("annualized_return", 0.0),
        status=payload.get("status", "rejected"),
        reason=payload.get("reason", ""),
    ))


def _flush_bucket(db: Session, accumulator: BucketAccumulator) -> None:
    """将分桶累加器刷入 SpreadBucket 表。"""
    row = db.query(SpreadBucket).filter(
        SpreadBucket.symbol == accumulator.symbol,
        SpreadBucket.direction == accumulator.direction,
        SpreadBucket.bucket_start == accumulator.bucket_start,
        SpreadBucket.bucket_seconds == accumulator.bucket_seconds,
    ).first()
    if not row:
        row = SpreadBucket(
            symbol=accumulator.symbol, direction=accumulator.direction,
            bucket_start=accumulator.bucket_start, bucket_seconds=accumulator.bucket_seconds,
        )
        db.add(row)
    row.open_spread = accumulator.open_spread
    row.high_spread = accumulator.high_spread
    row.low_spread = accumulator.low_spread
    row.close_spread = accumulator.close_spread
    row.avg_spread = accumulator.spread_sum / max(accumulator.sample_count, 1)
    row.entry_spread = accumulator.close_spread
    row.avg_entry_spread = row.avg_spread
    row.avg_close_basis_spread = accumulator.close_basis_sum / max(accumulator.sample_count, 1)
    row.avg_mid_spread = accumulator.mid_spread_sum / max(accumulator.sample_count, 1)
    row.avg_spread_cost = accumulator.spread_cost_sum / max(accumulator.sample_count, 1)
    row.avg_unit_cost = accumulator.unit_cost_sum / max(accumulator.sample_count, 1)
    row.avg_unit_net_profit = accumulator.unit_net_profit_sum / max(accumulator.sample_count, 1)
    row.sample_count = accumulator.sample_count


def _persist_opportunities(db: Session, opportunities: list[dict], ids_by_key: dict[tuple[str, str], int], scanned_symbols: set[str]) -> int:
    """同步套利机会到数据库。"""
    active_statuses = ("candidate", "executable", "executing")
    active_keys = {(str(row.get("symbol", "")).upper(), str(row.get("direction", ""))) for row in opportunities}
    changed = 0
    symbols = scanned_symbols or {symbol for symbol, _ in active_keys}
    if symbols:
        existing_rows = db.query(ArbitrageOpportunity).filter(
            ArbitrageOpportunity.symbol.in_(symbols), ArbitrageOpportunity.status.in_(active_statuses)
        ).all()
    else:
        existing_rows = db.query(ArbitrageOpportunity).filter(ArbitrageOpportunity.status.in_(active_statuses)).all()
    existing_by_key = {(row.symbol.upper(), row.direction): row for row in existing_rows}
    for payload in opportunities:
        key = (str(payload["symbol"]).upper(), str(payload["direction"]))
        current = existing_by_key.get(key)
        if not current:
            current = ArbitrageOpportunity(symbol=payload["symbol"], direction=payload["direction"])
            db.add(current)
            changed += 1
        elif current.status == "executing":
            ids_by_key[key] = current.id
            continue
        before = _opportunity_signature(current)
        current.notional = payload["notional"]
        current.quantity = payload["quantity"]
        current.leg_b_quantity = payload["leg_b_quantity"]
        current.leg_a_quantity = payload["leg_a_quantity"]
        current.notional_currency = payload["notional_currency"]
        current.fx_rate_to_usd = payload["fx_rate_to_usd"]
        current.gross_spread = payload["gross_spread"]
        current.trigger_leg_a_bid = payload["leg_a_bid"]
        current.trigger_leg_a_ask = payload["leg_a_ask"]
        current.trigger_leg_b_bid = payload["leg_b_bid"]
        current.trigger_leg_b_ask = payload["leg_b_ask"]
        current.unit_cost = payload["unit_cost"]
        current.unit_net_profit = payload["unit_net_profit"]
        current.total_cost = payload["total_cost"]
        current.net_profit = payload["net_profit"]
        current.annualized_return = payload["annualized_return"]
        current.entry_threshold = payload["entry_threshold"]
        current.exit_target = payload["exit_target"]
        current.overheat_threshold = payload["overheat_threshold"]
        current.signal_sample_count = payload["signal_sample_count"]
        current.status = payload["status"]
        current.reject_reason = payload.get("reason", "")
        if current.id is None:
            db.flush()
        if _opportunity_signature(current) != before:
            changed += 1
        ids_by_key[key] = current.id
    # 不再活跃的机会标记为 rejected
    for key, row in existing_by_key.items():
        if key in active_keys or row.status == "executing":
            continue
        row.status = "rejected"
        row.reject_reason = "价差回落，不再满足候选条件"
        changed += 1
    return changed


def _opportunity_signature(row: ArbitrageOpportunity) -> tuple:
    """机会签名，用于检测变更。"""
    return (
        row.status, row.gross_spread, row.unit_cost, row.unit_net_profit,
        row.total_cost, row.net_profit, row.entry_threshold, row.exit_target,
        row.overheat_threshold, row.signal_sample_count, row.reject_reason,
    )
