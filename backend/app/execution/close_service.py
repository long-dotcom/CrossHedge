"""
平仓结果应用模块
================

封装平仓结果的统一处理逻辑：
- 根据双边下单结果判断对冲组状态（成功 / 单边成交 / 待成交 / 失败）
- 更新对冲组的 status、closed_at、realized_pnl、fees 等字段
- 处理 "无可平仓位" 特殊情况的自动结案

使用方式::

    from app.execution.close_service import CloseResultConfig, apply_close_results

    apply_close_results(db, group, results, reason="...", config=CloseResultConfig(...))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import Alert, HedgeGroup, HedgeGroupEvent
from app.execution.manual_resolution import all_results_are_no_close_position, no_residual_position_close_reason
from app.execution.pnl import realized_pnl_from_fills

logger = get_logger(__name__)


@dataclass(frozen=True)
class CloseResultConfig:
    """平仓事件类型配置。"""
    success_event_type: str
    pending_event_type: str
    failed_event_type: str


def apply_close_results(
    db: Session,
    group: HedgeGroup,
    results: list[Any],
    *,
    reason: str,
    config: CloseResultConfig,
    manual_intervention: bool = False,
    estimated_realized_pnl: float | None = None,
) -> None:
    """根据下单结果更新对冲组状态。

    判断逻辑：
    1. 双边均成交 → 标记 closed
    2. 单边成交 → 标记 manual_intervention
    3. 有订单待成交 → 标记 closing
    4. 人工介入场景下所有结果均为 "无可平仓位" → 自动结案
    5. 其他 → 标记失败
    """
    if not results:
        raise ValueError("对冲组没有可平仓数量")

    if all(has_position_effect(result) for result in results):
        _mark_closed(db, group, results, reason, config.success_event_type, estimated_realized_pnl)
    elif any(has_position_effect(result) for result in results):
        group.status = "manual_intervention"
        group.close_reason = f"平仓单边成交: {reason}"
        db.add(Alert(level="critical", title="平仓单边成交", message=f"{group.symbol} 对冲组 #{group.id} 需要人工处理"))
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type="manual_intervention", detail=group.close_reason))
        logger.warning("平仓单边成交: symbol={}, group_id={}", group.symbol, group.id)
    elif any(is_pending_result(result) for result in results):
        group.status = "closing"
        group.close_reason = f"平仓订单待成交: {reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=config.pending_event_type, detail=group.close_reason))
    elif manual_intervention and all_results_are_no_close_position(results):
        # 人工介入场景：所有结果均为 "无可平仓位"，自动结案
        group.status = "closed"
        group.closed_at = utc_now()
        group.realized_pnl = group.realized_pnl or 0.0
        group.unrealized_pnl = 0.0
        group.close_reason = no_residual_position_close_reason(reason)
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=config.success_event_type, detail=group.close_reason))
        logger.info("无可平仓位自动结案: symbol={}, group_id={}", group.symbol, group.id)
    else:
        group.close_reason = f"平仓失败: {reason}"
        db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=config.failed_event_type, detail=group.close_reason))
        logger.warning("平仓失败: symbol={}, group_id={}, reason={}", group.symbol, group.id, reason)


def has_position_effect(result: Any) -> bool:
    """判断下单结果是否有成交（产生仓位变动）。"""
    return bool(
        getattr(result, "success", False)
        and float(getattr(result, "filled_quantity", 0.0) or 0.0) > 0
        and str(getattr(result, "status", "") or "") in {"filled", "partially_filled"}
    )


def is_pending_result(result: Any) -> bool:
    """判断下单结果是否为待成交状态。"""
    return str(getattr(result, "status", "") or "") in {"accepted", "submitted", "pending", "open", "new"}


def _mark_closed(
    db: Session,
    group: HedgeGroup,
    results: list[Any],
    reason: str,
    event_type: str,
    estimated_realized_pnl: float | None,
) -> None:
    """标记对冲组为已平仓，计算已实现盈亏。"""
    group.status = "closed"
    group.closed_at = utc_now()
    group.fees += sum(float(getattr(result, "fee", 0.0) or 0.0) for result in results)
    # 优先使用 Fill 记录计算已实现盈亏，其次使用估算值
    realized_from_fills = realized_pnl_from_fills(db, group)
    if realized_from_fills is not None:
        group.realized_pnl = realized_from_fills
    elif estimated_realized_pnl is not None:
        group.realized_pnl = estimated_realized_pnl
    else:
        group.realized_pnl = group.unrealized_pnl - group.fees - group.funding - group.swap
    group.unrealized_pnl = 0.0
    group.close_reason = reason
    db.add(HedgeGroupEvent(hedge_group_id=group.id, event_type=event_type, detail=reason))
    logger.info("对冲组平仓完成: symbol={}, group_id={}, reason={}", group.symbol, group.id, reason)
