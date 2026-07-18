"""对冲组服务端动作资格。

前端和写接口必须使用同一份资格判断，不能只凭模糊的业务状态决定是否平仓。
当前安全阶段仅允许已确认建立的 ``open/open_partial`` 组走普通平仓；异常组必须
先进入恢复流程，避免用计划数量误平不存在或不属于本组的真实仓位。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import ExecutionIntent, Fill, HedgeGroup, Order


NON_TERMINAL_ORDER_STATUSES = {
    "new", "initialized", "released", "emulated", "submitted", "accepted",
    "pending", "open", "pending_update", "pending_cancel", "partially_filled",
    "unknown", "not_ready",
}


def hedge_group_actions(db: Session, group: HedgeGroup | Any) -> dict[str, Any]:
    """返回对冲组当前允许的操作以及服务端原因。"""
    group_id = int(getattr(group, "id"))
    status = str(getattr(group, "status", "") or "")
    execution_mode = str(getattr(group, "execution_mode", "") or "")
    residuals = _fill_residuals(db, group_id)
    has_fill_exposure = any(quantity > 1e-12 for quantity in residuals.values())
    pending_count = (
        db.query(func.count(Order.id))
        .filter(
            Order.hedge_group_id == group_id,
            Order.status.in_(NON_TERMINAL_ORDER_STATUSES),
        )
        .scalar()
        or 0
    )
    active_intent = (
        db.query(ExecutionIntent.id, ExecutionIntent.status)
        .filter(
            ExecutionIntent.hedge_group_id == group_id,
            ExecutionIntent.status.in_({"CREATED", "RUNNING", "RECOVERY_REQUIRED"}),
        )
        .order_by(ExecutionIntent.id.desc())
        .first()
    )

    if active_intent is not None:
        close_allowed = False
        close_reason = f"已有活动执行 Intent #{active_intent.id} ({active_intent.status})"
    elif status in {"open", "open_partial"}:
        if execution_mode == "live" and not has_fill_exposure:
            close_allowed = False
            close_reason = "实盘组缺少可归属到本组的真实成交敞口，必须先对账"
        else:
            close_allowed = True
            close_reason = "已确认建立业务仓位"
    elif status == "manual_intervention":
        close_allowed = False
        close_reason = "异常组必须先生成恢复计划，禁止按原计划数量直接平仓"
    else:
        close_allowed = False
        close_reason = f"当前状态 {status or 'unknown'} 不允许普通平仓"

    recover_allowed = status in {"manual_intervention", "failed"} and (has_fill_exposure or pending_count > 0)
    reconcile_allowed = pending_count > 0 or status in {"opening", "closing", "manual_intervention"}
    exposure_state = (
        "EXPOSURE_CONFIRMED" if has_fill_exposure
        else "EXPOSURE_UNKNOWN" if pending_count > 0 or status == "manual_intervention"
        else "NO_EXPOSURE_CONFIRMED"
    )
    return {
        "exposure_state": exposure_state,
        "residual_fill_quantities": residuals,
        "pending_order_count": int(pending_count),
        "active_intent_id": active_intent.id if active_intent else None,
        "close": {"allowed": close_allowed, "reason": close_reason},
        "reconcile": {
            "allowed": reconcile_allowed,
            "reason": "存在待确认订单或异常执行状态" if reconcile_allowed else "没有待确认执行状态",
        },
        "recover": {
            "allowed": recover_allowed,
            "reason": "存在异常敞口或待确认订单" if recover_allowed else "当前没有可执行的恢复计划",
        },
    }


def require_group_action(db: Session, group: HedgeGroup | Any, action: str) -> None:
    """校验服务端动作资格，不允许 API 绕过前端限制。"""
    actions = hedge_group_actions(db, group)
    decision = actions.get(action) or {"allowed": False, "reason": "未知操作"}
    if not bool(decision.get("allowed")):
        raise ValueError(str(decision.get("reason") or "当前操作不允许"))


def _fill_residuals(db: Session, group_id: int) -> dict[str, float]:
    """按平台计算归属于本组的开仓 Fill 减平仓 Fill。"""
    rows = (
        db.query(Order.platform, Order.reduce_only, func.coalesce(func.sum(Fill.quantity), 0.0))
        .join(Fill, Fill.order_id == Order.id)
        .filter(Order.hedge_group_id == group_id)
        .group_by(Order.platform, Order.reduce_only)
        .all()
    )
    values: dict[str, float] = defaultdict(float)
    for platform, is_close, quantity in rows:
        delta = float(quantity or 0.0)
        values[str(platform)] += -delta if bool(is_close) else delta
    return {platform: max(quantity, 0.0) for platform, quantity in values.items()}


def group_fill_residuals(db: Session, group_id: int) -> dict[str, float]:
    """公开本组按 venue 归属的剩余成交量，供 Coordinator 生成真实平仓计划。"""
    return _fill_residuals(db, group_id)
