"""
对冲池事件持久化模块
====================

将内存中对冲池（``hedge_pool``）累积的平仓结果事件批量写入数据库：
- 从 ``hedge_pool`` 中取出待持久化的 ``CloseResultEvent``
- 逐条写入 Order / Fill / HedgeGroup 状态变更 / HedgeGroupEvent
- 失败时记录 SystemLog 并将未处理事件重新入队

使用 ``run_worker`` 模板自动记录 WorkerRun 和执行 prune，
消除手写 WorkerRun + SystemLog + prune + commit 样板代码。

使用方式::

    from app.core.db_session import db_session
    from app.execution.persistence import persist_hedge_pool_events

    with db_session() as db:
        count = persist_hedge_pool_events(db)
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.core.worker_runner import run_worker
from app.db.models import Fill, HedgeGroup, HedgeGroupEvent, Order, SystemLog, WorkerRun
from app.execution.hedge_pool import CloseResultEvent, hedge_pool

logger = get_logger(__name__)


def persist_hedge_pool_events(db: Session, *, limit: int = 100) -> int:
    """将内存对冲池中的平仓结果事件持久化到数据库。

    使用 ``run_worker`` 模板执行，自动记录 WorkerRun 并在完成后
    对 WorkerRun / SystemLog 表执行 prune 防止膨胀。

    参数:
        db: 数据库会话。
        limit: 单次最多处理的事件数量，默认 100。

    返回:
        成功持久化的事件数量。
    """
    return run_worker(
        db,
        "hedge_pool_persistence",
        lambda db: _persist_impl(db, limit=limit),
        prune_models=[WorkerRun, SystemLog],
    )


def _persist_impl(db: Session, *, limit: int) -> int:
    """内部实现：逐条持久化对冲池事件。

    每条事件独立 commit，失败时记录 SystemLog 并将剩余事件重新入队。

    参数:
        db: 数据库会话。
        limit: 单次最多处理的事件数量。

    返回:
        成功持久化的事件数量。
    """
    events = hedge_pool.drain_close_results(limit)
    if not events:
        return 0
    processed = 0
    failed: list[CloseResultEvent] = []
    for event in events:
        try:
            _persist_close_result(db, event)
            db.commit()
            processed += 1
        except Exception as exc:
            db.rollback()
            failed.append(event)
            # 记录警告日志到 SystemLog 表
            db.add(SystemLog(
                level="warning",
                category="hedge_pool_persistence",
                message=f"对冲池事件落库失败: #{event.group_id}",
                context=str(exc),
            ))
            db.commit()
            break
    # 将失败及未处理的事件重新放回对冲池
    if failed:
        hedge_pool.requeue_close_results(failed + events[processed + len(failed):])
    return processed


def _persist_close_result(db: Session, event: CloseResultEvent) -> None:
    """持久化单条平仓结果事件。

    将事件中的订单（Order）和成交（Fill）写入数据库，
    同步更新对冲组的状态、盈亏、关闭时间等字段。

    参数:
        db: 数据库会话。
        event: 待持久化的平仓结果事件。

    异常:
        ValueError: 对冲组不存在时抛出。
    """
    group = db.get(HedgeGroup, event.group_id)
    if not group:
        raise ValueError("对冲组不存在，无法持久化内存事件")
    # 逐条写入订单和成交记录
    for item in event.orders:
        order = Order(
            hedge_group_id=event.group_id,
            platform=item.platform,
            symbol=item.symbol,
            side=item.side,
            quantity=item.quantity,
            order_type=item.order_type,
            price=item.average_price or item.price,
            post_only=item.post_only,
            reduce_only=item.reduce_only,
            ttl_seconds=item.ttl_seconds,
            status=item.status,
            external_order_id=item.external_order_id,
            error_message=item.error_message,
        )
        db.add(order)
        db.flush()  # 获取 order.id 用于关联 Fill
        for fill in item.fills:
            db.add(Fill(
                order_id=order.id,
                platform=fill.platform,
                symbol=fill.symbol,
                side=fill.side,
                quantity=fill.quantity,
                price=fill.price,
                fee=fill.fee,
            ))
    # 同步对冲组状态
    group.status = event.status
    group.close_reason = event.close_reason
    if event.unrealized_pnl is not None:
        group.unrealized_pnl = event.unrealized_pnl
    if event.realized_pnl is not None:
        group.realized_pnl = event.realized_pnl
    if event.fees_delta:
        group.fees = float(group.fees or 0.0) + event.fees_delta
    if event.closed_at:
        group.closed_at = event.closed_at
    elif event.status == "closed":
        group.closed_at = utc_now()
    # 记录事件
    db.add(HedgeGroupEvent(
        hedge_group_id=event.group_id,
        event_type=event.event_type,
        detail=event.event_detail,
    ))
