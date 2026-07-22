"""对冲组执行 Intent 协调器。

API 只调用本模块创建不可变业务意图，不直接访问交易所。Coordinator 在同一事务
内完成动作资格、幂等、活动 Intent 互斥、真实残量计算、状态占位和 Outbox 写入。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.db.models import ArbitrageOpportunity, ExecutionIntent, HedgeGroup, HedgeGroupEvent, StrategySetting, SymbolMapping
from app.execution.actions import group_fill_residuals, hedge_group_actions, require_group_action
from app.execution.intents import ExecutionLegPlan, IntentCreationResult, create_execution_intent
from app.execution.modes import (
    MAKER_THEN_MARKET, execution_mode, maker_leg, maker_offset_bps,
    maker_ttl_seconds, maker_unfilled_action,
)
from app.market.mt5_sessions import mt5_action_allowed, mt5_session_state
from app.risk.engine import pre_trade_check, record_risk_event


ACTIVE_INTENT_STATUSES = {"CREATED", "RUNNING", "RECOVERY_REQUIRED"}


def create_open_intent(
    db: Session,
    *,
    opportunity_id: int,
    requested_by: str,
    idempotency_key: str,
    source: str = "system",
    force_strategy_checks: bool = False,
) -> IntentCreationResult:
    """完成开仓预检并创建异步 OPEN Intent，不在请求线程下单。"""
    from app.execution.preflight import (
        OPEN_CAPACITY_LOCK,
        refreshed_opportunity_still_executable,
        require_live_execution_ready,
        require_paper_execution_ready,
        strict_sync_for_execution,
        live_trading_enabled,
    )

    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise ValueError("开仓请求必须提供 Idempotency-Key")
    existing = (
        db.query(ExecutionIntent)
        .filter(ExecutionIntent.idempotency_key == normalized_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.intent_type != "OPEN":
            raise ValueError("Idempotency-Key 已被其他执行请求使用")
        return IntentCreationResult(existing, False)

    with OPEN_CAPACITY_LOCK:
        opportunity = db.get(ArbitrageOpportunity, opportunity_id)
        if opportunity is None:
            raise ValueError("机会不存在")
        if opportunity.status not in {"executable", "executing"}:
            raise ValueError("只有 executable/executing 状态的机会允许执行")
        strategy = db.query(StrategySetting).first() or StrategySetting()
        mode = str(strategy.execution_mode or "paper").lower()
        if mode not in {"paper", "live"}:
            raise ValueError(f"不支持的执行模式: {mode}")
        if mode == "live":
            if not live_trading_enabled(db):
                raise ValueError("实盘交易总开关未开启")
            require_live_execution_ready(db)
        else:
            require_paper_execution_ready(db)

        mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == opportunity.symbol).one_or_none()
        if mapping is None:
            raise ValueError("品种映射不存在")
        if mode == "paper":
            _require_hybrid_paper_mapping(mapping)
        style = execution_mode(mapping)
        if style == MAKER_THEN_MARKET:
            maker_key = maker_leg(mapping)
            maker_venue = mapping.leg_a_venue if maker_key == "a" else mapping.leg_b_venue
            if maker_venue == "mt5":
                raise ValueError("MT5 当前不支持 Post-only，不能作为 Maker 腿")

        mt5_leg = "a" if mapping.leg_a_venue == "mt5" else "b" if mapping.leg_b_venue == "mt5" else ""
        if mt5_leg:
            session_state = mt5_session_state(mapping)
            mt5_allowed, mt5_reason = mt5_action_allowed(session_state, opportunity.direction, "open")
            if not mt5_allowed:
                opportunity.reject_reason = mt5_reason
                record_risk_event(db, "mt5_session_open", mt5_reason, opportunity.symbol)
                raise ValueError(mt5_reason)
            if mode == "live":
                from app.adapters.mt5 import mt5_market_order_check
                from app.market.mt5_tradability import mt5_tradability_cache

                leg_a_side = "buy" if opportunity.direction == "long_leg_a_short_leg_b" else "sell"
                leg_b_side = "sell" if opportunity.direction == "long_leg_a_short_leg_b" else "buy"
                mt5_symbol = mapping.leg_a_venue_symbol if mt5_leg == "a" else mapping.mt5_symbol
                mt5_side = leg_a_side if mt5_leg == "a" else leg_b_side
                mt5_quantity = (
                    (opportunity.leg_a_quantity or opportunity.quantity)
                    if mt5_leg == "a"
                    else (opportunity.leg_b_quantity or opportunity.quantity)
                )
                mt5_check = mt5_market_order_check(mt5_symbol, mt5_side, mt5_quantity, demo=False)
                mt5_tradability_cache.update(
                    opportunity.symbol, mt5_symbol, mt5_side, mt5_quantity, mt5_check, "execution_intent",
                )
                if not mt5_check.allowed:
                    reason = f"MT5 当前订单预检查失败: {mt5_check.message}"
                    opportunity.reject_reason = reason
                    record_risk_event(db, "mt5_order_check_open", reason, opportunity.symbol)
                    raise ValueError(reason)

        settings = get_settings()
        synced, sync_reason, refreshed = strict_sync_for_execution(mapping, opportunity.symbol, settings)
        if not synced:
            record_risk_event(db, "strict_quote_sync", sync_reason, opportunity.symbol)
            raise ValueError(sync_reason)
        if refreshed:
            still_executable, refresh_reason = refreshed_opportunity_still_executable(opportunity, synced, strategy)
            if not still_executable and not force_strategy_checks:
                record_risk_event(db, "execution_quote_refresh", refresh_reason, opportunity.symbol)
                raise ValueError(refresh_reason)
        use_live_account_risk = mode == "live" or (mode == "paper" and strategy.paper_use_live_account_risk)
        slippage_bps = settings.cost.default_slippage_bps if refreshed else synced.time_diff_ms / 10
        decision = pre_trade_check(
            db,
            opportunity.symbol,
            opportunity.notional,
            slippage_bps,
            synced.leg_a.local_recv_ts,
            use_live_account_risk=use_live_account_risk,
            direction=opportunity.direction,
        )
        if not decision.allowed:
            record_risk_event(db, "pre_trade", decision.reason, opportunity.symbol)
            raise ValueError(decision.reason)

        group = HedgeGroup(
            symbol=opportunity.symbol,
            direction=opportunity.direction,
            status="opening",
            execution_mode=mode,
            execution_profile="hybrid_probe_demo" if mode == "paper" else "live",
            notional=opportunity.notional,
            quantity=opportunity.quantity,
            leg_b_quantity=opportunity.leg_b_quantity or opportunity.quantity,
            leg_a_quantity=opportunity.leg_a_quantity or opportunity.quantity,
            open_cost=opportunity.total_cost,
            trigger_spread=opportunity.gross_spread,
            trigger_leg_a_bid=opportunity.trigger_leg_a_bid,
            trigger_leg_a_ask=opportunity.trigger_leg_a_ask,
            trigger_leg_b_bid=opportunity.trigger_leg_b_bid,
            trigger_leg_b_ask=opportunity.trigger_leg_b_ask,
            # 真实开仓价差只能在双腿成交回报到齐后写入，0 表示尚未确认。
            entry_spread=0.0,
            entry_threshold=opportunity.entry_threshold,
            exit_target=opportunity.exit_target,
            overheat_threshold=opportunity.overheat_threshold,
            source=source,
        )
        db.add(group)
        db.flush()
        leg_plans = _open_leg_plans(group, mapping)
        command_payload = {
            "opportunity_id": opportunity.id,
            "coordinator": "hedge_group_open_v2",
        }
        if style == MAKER_THEN_MARKET:
            leg_plans, maker_payload = _maker_stage_plans(mapping, leg_plans, synced)
            command_payload.update(maker_payload)
        result = create_execution_intent(
            db,
            intent_type="OPEN",
            execution_mode=mode,
            execution_style=style,
            idempotency_key=normalized_key,
            hedge_group_id=group.id,
            requested_by=requested_by,
            legs=leg_plans,
            command_payload=command_payload,
        )
        opportunity.status = "executing"
        opportunity.reject_reason = f"OPEN Intent #{result.intent.id} 已创建，等待成交确认"
        db.add(HedgeGroupEvent(
            hedge_group_id=group.id,
            event_type="open_intent_created",
            detail=f"Intent #{result.intent.id}; opportunity=#{opportunity.id}",
        ))
        db.flush()
        return result


def _open_leg_plans(group: HedgeGroup, mapping: SymbolMapping) -> list[ExecutionLegPlan]:
    leg_a_venue = str(mapping.leg_a_venue or "hyperliquid").strip().lower()
    leg_b_venue = str(mapping.leg_b_venue or "mt5").strip().lower()
    leg_a_symbol = str(mapping.leg_a_symbol or mapping.leg_a_venue_symbol or group.symbol)
    leg_b_symbol = str(mapping.leg_b_symbol or mapping.mt5_symbol or group.symbol)
    if group.direction == "long_leg_a_short_leg_b":
        sides = {"a": ("BUY", "LONG"), "b": ("SELL", "SHORT")}
    elif group.direction == "long_leg_b_short_leg_a":
        sides = {"a": ("SELL", "SHORT"), "b": ("BUY", "LONG")}
    else:
        raise ValueError(f"未知对冲方向: {group.direction}")
    definitions = (
        ("leg_a", "a", leg_a_venue, leg_a_symbol, float(group.leg_a_quantity or group.quantity)),
        ("leg_b", "b", leg_b_venue, leg_b_symbol, float(group.leg_b_quantity or group.quantity)),
    )
    return [
        ExecutionLegPlan(
            leg_key=leg_key,
            venue=venue,
            instrument_id=venue_symbol,
            venue_symbol=venue_symbol,
            action="OPEN",
            position_side=sides[index][1],
            order_side=sides[index][0],
            strategy_quantity=quantity,
            venue_order_quantity=quantity,
            target_position_quantity_before=0.0,
            target_position_quantity_after=quantity,
            order_type="market",
            venue_reduce_only=False,
        )
        for leg_key, index, venue, venue_symbol, quantity in definitions
        if quantity > 1e-12
    ]


def _maker_stage_plans(
    mapping: SymbolMapping,
    plans: list[ExecutionLegPlan],
    synced: object,
) -> tuple[list[ExecutionLegPlan], dict]:
    """把双腿计划拆成首个 Maker 命令和持久化的 Hedge 模板。"""
    from dataclasses import asdict, replace
    from app.execution.preflight import maker_price

    maker_index = 0 if maker_leg(mapping) == "a" else 1
    if len(plans) != 2:
        raise ValueError("Maker-then-Market 要求两条有效执行腿")
    maker_plan = plans[maker_index]
    hedge_plan = plans[1 - maker_index]
    quote = synced.leg_a if maker_index == 0 else synced.leg_b
    price = maker_price(
        maker_plan.order_side.lower(),
        float(quote.bid),
        float(quote.ask),
        maker_offset_bps(mapping),
        mapping,
    )
    if price <= 0:
        raise ValueError("Maker 限价计算失败")
    maker_plan = replace(
        maker_plan,
        order_type="limit",
        limit_price=price,
        time_in_force="GTC",
        post_only=True,
        role="MAKER",
        sequence=0,
    )
    hedge_plan = replace(
        hedge_plan,
        order_type="market",
        limit_price=None,
        post_only=False,
        role="HEDGE",
        sequence=1,
    )
    return [maker_plan], {
        "maker_state_version": 1,
        "maker_target_quantity": maker_plan.venue_order_quantity,
        "hedge_target_quantity": hedge_plan.venue_order_quantity,
        "hedge_template": asdict(hedge_plan),
        "maker_ttl_seconds": maker_ttl_seconds(mapping),
        "maker_unfilled_action": maker_unfilled_action(mapping),
        "single_leg_action": str(getattr(mapping, "single_leg_action", "manual_intervention") or "manual_intervention"),
    }


def create_close_intent(
    db: Session,
    *,
    group_id: int,
    reason: str,
    requested_by: str,
    idempotency_key: str,
) -> IntentCreationResult:
    """为普通平仓创建幂等 CLOSE Intent，不在请求线程下单。"""
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise ValueError("平仓请求必须提供 Idempotency-Key")
    existing = (
        db.query(ExecutionIntent)
        .filter(ExecutionIntent.idempotency_key == normalized_key)
        .one_or_none()
    )
    if existing is not None:
        if existing.hedge_group_id != group_id or existing.intent_type != "CLOSE":
            raise ValueError("Idempotency-Key 已被其他执行请求使用")
        return IntentCreationResult(existing, False)

    group = db.get(HedgeGroup, group_id)
    if group is None:
        raise ValueError("对冲组不存在")
    _require_hybrid_paper_group(group)
    require_group_action(db, group, "close")
    active = (
        db.query(ExecutionIntent)
        .filter(
            ExecutionIntent.hedge_group_id == group_id,
            ExecutionIntent.status.in_(ACTIVE_INTENT_STATUSES),
        )
        .order_by(ExecutionIntent.id.desc())
        .first()
    )
    if active is not None:
        raise ValueError(f"对冲组已有活动执行 Intent #{active.id} ({active.status})")

    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).one_or_none()
    if mapping is None:
        raise ValueError("品种映射不存在")
    previous_status = group.status
    style = execution_mode(mapping)
    legs = _close_leg_plans(db, group, mapping)
    if not legs:
        raise ValueError("没有可归属到本组的剩余仓位，禁止生成平仓订单")
    command_payload = {
        "reason": reason.strip() or "manual close",
        "previous_group_status": previous_status,
        "coordinator": "hedge_group_close_v2",
    }
    if style == MAKER_THEN_MARKET:
        if (mapping.leg_a_venue if maker_leg(mapping) == "a" else mapping.leg_b_venue) == "mt5":
            raise ValueError("MT5 当前不支持 Post-only，不能作为 Maker 腿")
        from app.execution.preflight import strict_sync_for_execution

        synced, sync_reason, _ = strict_sync_for_execution(mapping, group.symbol, get_settings())
        if not synced:
            raise ValueError(f"Maker 平仓前报价同步失败: {sync_reason}")
        legs, maker_payload = _maker_stage_plans(mapping, legs, synced)
        command_payload.update(maker_payload)

    result = create_execution_intent(
        db,
        intent_type="CLOSE",
        execution_mode=group.execution_mode,
        execution_style=style,
        idempotency_key=normalized_key,
        hedge_group_id=group.id,
        requested_by=requested_by,
        legs=legs,
        command_payload=command_payload,
    )
    group.status = "closing"
    group.close_reason = f"平仓 Intent #{result.intent.id} 已创建，等待 venue 成交确认"
    db.add(HedgeGroupEvent(
        hedge_group_id=group.id,
        event_type="close_intent_created",
        detail=f"Intent #{result.intent.id}; reason={reason.strip() or 'manual close'}",
    ))
    db.flush()
    return result


def create_recovery_intent(
    db: Session,
    *,
    group_id: int,
    reason: str,
    requested_by: str,
    idempotency_key: str,
) -> IntentCreationResult:
    """按本组已确认 Fill 残量创建独立恢复 Intent。

    恢复操作不会复用原计划数量；仍有未确认订单时必须先对账，避免迟到成交后
    恢复单把仓位反向开出。旧的 ``RECOVERY_REQUIRED`` Intent 会被明确取代。
    """
    normalized_key = idempotency_key.strip()
    if not normalized_key:
        raise ValueError("恢复请求必须提供 Idempotency-Key")
    existing = db.query(ExecutionIntent).filter(
        ExecutionIntent.idempotency_key == normalized_key,
    ).one_or_none()
    if existing is not None:
        if existing.hedge_group_id != group_id or existing.intent_type != "RECOVER":
            raise ValueError("Idempotency-Key 已被其他执行请求使用")
        return IntentCreationResult(existing, False)

    group = db.get(HedgeGroup, group_id)
    if group is None:
        raise ValueError("对冲组不存在")
    _require_hybrid_paper_group(group)
    require_group_action(db, group, "recover")
    actions = hedge_group_actions(db, group)
    if int(actions.get("pending_order_count") or 0) > 0:
        raise ValueError("仍有未确认订单，必须先同步到终态后才能生成恢复 Intent")

    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).one_or_none()
    if mapping is None:
        raise ValueError("品种映射不存在")
    legs = _close_leg_plans(db, group, mapping)
    if not legs:
        raise ValueError("本组没有已确认成交残量，禁止生成恢复订单")

    superseded = db.query(ExecutionIntent).filter(
        ExecutionIntent.hedge_group_id == group.id,
        ExecutionIntent.status == "RECOVERY_REQUIRED",
    ).all()
    result = create_execution_intent(
        db,
        intent_type="RECOVER",
        execution_mode=group.execution_mode,
        execution_style="simultaneous_market",
        idempotency_key=normalized_key,
        hedge_group_id=group.id,
        requested_by=requested_by,
        legs=legs,
        command_payload={
            "reason": reason.strip() or "manual recovery flatten",
            "previous_group_status": group.status,
            "coordinator": "hedge_group_recovery_v1",
            "superseded_intent_ids": [row.id for row in superseded],
        },
    )
    for row in superseded:
        row.status = "SUPERSEDED"
        row.error_message = f"已由恢复 Intent #{result.intent.id} 取代"
    group.status = "closing"
    group.close_reason = f"恢复 Intent #{result.intent.id} 已创建，仅回平本组确认成交残量"
    db.add(HedgeGroupEvent(
        hedge_group_id=group.id,
        event_type="recovery_intent_created",
        detail=f"Intent #{result.intent.id}; residuals={actions.get('residual_fill_quantities')}; reason={reason}",
    ))
    db.flush()
    return result


def _close_leg_plans(db: Session, group: HedgeGroup, mapping: SymbolMapping) -> list[ExecutionLegPlan]:
    residuals = group_fill_residuals(db, group.id)
    leg_a_venue = str(mapping.leg_a_venue or "hyperliquid").strip().lower()
    leg_b_venue = str(mapping.leg_b_venue or "mt5").strip().lower()
    leg_a_symbol = str(mapping.leg_a_symbol or mapping.leg_a_venue_symbol or group.symbol)
    leg_b_symbol = str(mapping.leg_b_symbol or mapping.mt5_symbol or group.symbol)

    if group.direction == "long_leg_a_short_leg_b":
        sides = {"a": ("SELL", "LONG"), "b": ("BUY", "SHORT")}
    elif group.direction == "long_leg_b_short_leg_a":
        sides = {"a": ("BUY", "SHORT"), "b": ("SELL", "LONG")}
    else:
        raise ValueError(f"未知对冲方向: {group.direction}")

    definitions = (
        ("leg_a", "a", leg_a_venue, leg_a_symbol, float(group.leg_a_quantity or group.quantity or 0.0)),
        ("leg_b", "b", leg_b_venue, leg_b_symbol, float(group.leg_b_quantity or group.quantity or 0.0)),
    )
    plans: list[ExecutionLegPlan] = []
    for leg_key, index, venue, venue_symbol, strategy_quantity in definitions:
        # live 必须以本组 Fill 残量为准；paper 使用模拟账本计划量。
        venue_quantity = float(residuals.get(venue, 0.0)) if group.execution_mode == "live" else strategy_quantity
        if venue_quantity <= 1e-12:
            continue
        order_side, position_side = sides[index]
        plans.append(ExecutionLegPlan(
            leg_key=leg_key,
            venue=venue,
            instrument_id=venue_symbol,
            venue_symbol=venue_symbol,
            action="CLOSE",
            position_side=position_side,
            order_side=order_side,
            strategy_quantity=strategy_quantity,
            venue_order_quantity=venue_quantity,
            target_position_quantity_before=venue_quantity,
            target_position_quantity_after=0.0,
            order_type="market",
            time_in_force="GTC",
            post_only=False,
            venue_reduce_only=venue != "binance",
        ))
    return plans


def _require_hybrid_paper_mapping(mapping: SymbolMapping) -> None:
    """Paper 只允许一条加密探针腿和一条 MT5 Demo 腿。"""
    venues = {
        str(mapping.leg_a_venue or "").strip().lower(),
        str(mapping.leg_b_venue or "").strip().lower(),
    }
    crypto = venues & {"hyperliquid", "binance"}
    if "mt5" not in venues or len(crypto) != 1 or len(venues) != 2:
        raise ValueError("Paper 仅支持一条 Hyperliquid/Binance 真实最小探针腿与一条 MT5 Demo 腿")


def _require_hybrid_paper_group(group: HedgeGroup) -> None:
    """阻止旧本地 Paper 记录误触发真实探针或 MT5 Demo 订单。"""
    if str(group.execution_mode or "").lower() != "paper":
        return
    if str(getattr(group, "execution_profile", "legacy_local") or "legacy_local") != "hybrid_probe_demo":
        raise ValueError("该对冲组属于旧本地 Paper，禁止通过混合探针链路平仓或恢复")
