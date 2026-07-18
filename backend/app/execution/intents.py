"""执行意图与 Outbox 的事务写入服务。

本模块只负责持久化业务意图，不直接调用交易所。调用方应在同一个数据库事务中
提交 ``ExecutionIntent``、各执行腿和 ``ExecutionOutbox``，随后由独立执行 Worker
领取命令。相同幂等键始终返回第一次创建的意图，避免接口重试造成重复下单。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db.models import ExecutionIntent, ExecutionLeg, ExecutionOutbox


@dataclass(frozen=True, slots=True)
class ExecutionLegPlan:
    """一条已经完成风控与数量换算的执行腿计划。"""

    leg_key: str
    venue: str
    instrument_id: str
    venue_symbol: str
    action: str
    position_side: str
    order_side: str
    strategy_quantity: float
    venue_order_quantity: float
    target_position_quantity_before: float | None = None
    target_position_quantity_after: float | None = None
    order_type: str = "market"
    limit_price: float | None = None
    time_in_force: str = "GTC"
    post_only: bool = False
    venue_reduce_only: bool = False
    role: str = "PRIMARY"
    parent_leg_id: int | None = None
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class IntentCreationResult:
    """意图创建结果；``created=False`` 表示命中了已有幂等记录。"""

    intent: ExecutionIntent
    created: bool


def create_execution_intent(
    db: Session,
    *,
    intent_type: str,
    idempotency_key: str,
    legs: Iterable[ExecutionLegPlan],
    hedge_group_id: int | None = None,
    requested_by: str = "system",
    execution_mode: str = "paper",
    execution_style: str = "simultaneous_market",
    expected_group_version: int = 0,
    command_payload: dict[str, Any] | None = None,
) -> IntentCreationResult:
    """在调用方事务中原子创建 Intent、Leg 和待投递命令。

    此函数仅 ``flush``，不会自行 ``commit``。这样业务表变更可与执行意图一起提交，
    任一部分失败都会整体回滚。
    """
    normalized_type = intent_type.strip().upper()
    normalized_key = idempotency_key.strip()
    normalized_mode = execution_mode.strip().lower()
    normalized_style = execution_style.strip().lower()
    leg_plans = tuple(legs)
    _validate_intent(normalized_type, normalized_key, leg_plans)
    if normalized_mode not in {"paper", "live"}:
        raise ValueError("execution_mode 仅支持 paper/live")
    if normalized_style not in {"simultaneous_market", "maker_then_market"}:
        raise ValueError("execution_style 仅支持 simultaneous_market/maker_then_market")

    existing = _intent_by_key(db, normalized_key)
    if existing is not None:
        return IntentCreationResult(existing, False)

    intent = ExecutionIntent(
        hedge_group_id=hedge_group_id,
        intent_type=normalized_type,
        execution_mode=normalized_mode,
        execution_style=normalized_style,
        requested_by=requested_by.strip() or "system",
        idempotency_key=normalized_key,
        status="CREATED",
        expected_group_version=max(0, int(expected_group_version)),
    )

    db.add(intent)
    db.flush()
    leg_rows: list[ExecutionLeg] = []
    for plan in leg_plans:
        row = ExecutionLeg(intent_id=intent.id, status="PLANNED", **asdict(plan))
        db.add(row)
        leg_rows.append(row)
    db.flush()

    payload = {
        "schema_version": 1,
        "intent_id": intent.id,
        "intent_type": normalized_type,
        "execution_mode": normalized_mode,
        "execution_style": normalized_style,
        "idempotency_key": normalized_key,
        "hedge_group_id": hedge_group_id,
        "legs": [asdict(plan) for plan in leg_plans],
        "dispatch_leg_ids": [row.id for row in leg_rows],
        **(command_payload or {}),
    }
    db.add(
        ExecutionOutbox(
            intent_id=intent.id,
            event_type="EXECUTION_INTENT_CREATED",
            payload=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            status="PENDING",
        )
    )
    db.flush()

    return IntentCreationResult(intent, True)


def enqueue_execution_leg(
    db: Session,
    *,
    intent: ExecutionIntent,
    plan: ExecutionLegPlan,
    event_type: str,
    command_payload: dict[str, Any] | None = None,
) -> ExecutionLeg:
    """为活动 Intent 原子追加一条动态执行腿及其独立 Outbox 命令。

    Maker 增量对冲、TTL 市价兜底和补偿都必须通过本入口生成新的稳定腿，不能
    修改或复用已经提交的腿。调用方负责与状态推进在同一事务提交。
    """
    if intent.status not in {"CREATED", "RUNNING", "RECOVERY_REQUIRED"}:
        raise ValueError(f"Intent #{intent.id} 已是终态，禁止追加执行腿")
    if db.query(ExecutionLeg.id).filter(
        ExecutionLeg.intent_id == intent.id,
        ExecutionLeg.leg_key == plan.leg_key,
    ).first() is not None:
        raise ValueError(f"Intent #{intent.id} 已存在执行腿 {plan.leg_key}")
    _validate_intent(intent.intent_type, intent.idempotency_key, (plan,))
    row = ExecutionLeg(intent_id=intent.id, status="PLANNED", **asdict(plan))
    db.add(row)
    db.flush()
    payload = {
        "schema_version": 1,
        "intent_id": intent.id,
        "intent_type": intent.intent_type,
        "execution_mode": intent.execution_mode,
        "execution_style": intent.execution_style,
        "idempotency_key": intent.idempotency_key,
        "hedge_group_id": intent.hedge_group_id,
        "legs": [asdict(plan)],
        "dispatch_leg_ids": [row.id],
        **(command_payload or {}),
    }
    db.add(ExecutionOutbox(
        intent_id=intent.id,
        event_type=event_type.strip().upper() or "EXECUTION_LEG_CREATED",
        payload=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        status="PENDING",
    ))
    db.flush()
    return row


def _intent_by_key(db: Session, idempotency_key: str) -> ExecutionIntent | None:
    return (
        db.query(ExecutionIntent)
        .filter(ExecutionIntent.idempotency_key == idempotency_key)
        .one_or_none()
    )


def _validate_intent(
    intent_type: str,
    idempotency_key: str,
    legs: tuple[ExecutionLegPlan, ...],
) -> None:
    if not intent_type:
        raise ValueError("intent_type 不能为空")
    if not idempotency_key:
        raise ValueError("idempotency_key 不能为空")
    if not legs:
        raise ValueError("执行意图至少需要一条执行腿")

    leg_keys = [plan.leg_key.strip() for plan in legs]
    if any(not key for key in leg_keys):
        raise ValueError("leg_key 不能为空")
    if len(set(leg_keys)) != len(leg_keys):
        raise ValueError("同一执行意图中的 leg_key 不可重复")

    for plan in legs:
        if not plan.venue.strip() or not plan.venue_symbol.strip():
            raise ValueError(f"执行腿 {plan.leg_key} 缺少 venue 或 venue_symbol")
        if plan.strategy_quantity <= 0 or plan.venue_order_quantity <= 0:
            raise ValueError(f"执行腿 {plan.leg_key} 的数量必须大于 0")
