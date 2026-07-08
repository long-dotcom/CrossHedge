"""
对冲组内存池模块
================

维护对冲组的内存快照缓存，避免每次操作都查询数据库：
- ``HedgeGroupSnapshot`` —— 对冲组的不可变内存快照
- ``CloseResultEvent`` —— 平仓结果事件（待持久化）
- ``HedgePoolStore`` —— 线程安全的对冲组内存池

内存池用于自动平仓、自动执行等高频读取场景，
数据库操作完成后通过 ``upsert_group`` 同步回内存池。

使用方式::

    from app.execution.hedge_pool import hedge_pool

    hedge_pool.load_from_db(db)
    open_groups = hedge_pool.snapshot_open_groups(["paper"])
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Deque, Iterable

from sqlalchemy.orm import Session

from app.core.time_utils import utc_now
from app.db.models import HedgeGroup


# 内存池管理的对冲组状态集合
POOL_GROUP_STATUSES = {"opening", "open", "open_partial", "closing", "manual_intervention"}
# 允许自动平仓的状态集合
AUTO_CLOSE_STATUSES = {"open", "open_partial"}


@dataclass(frozen=True)
class HedgeGroupSnapshot:
    """对冲组的不可变内存快照。

    使用 ``dataclass(frozen=True)`` 确保不可变性，
    更新时通过 ``with_updates()`` 返回新实例。
    """
    id: int
    symbol: str
    direction: str
    status: str
    execution_mode: str
    notional: float
    quantity: float
    leg_b_quantity: float
    leg_a_quantity: float
    open_cost: float
    fees: float
    funding: float
    swap: float
    realized_pnl: float
    unrealized_pnl: float
    trigger_spread: float
    entry_spread: float
    entry_threshold: float
    exit_target: float
    overheat_threshold: float
    close_reason: str
    opened_at: datetime | None
    closed_at: datetime | None
    source: str

    @classmethod
    def from_row(cls, row: HedgeGroup) -> "HedgeGroupSnapshot":
        """从数据库 ORM 行对象创建快照。"""
        return cls(
            id=int(row.id),
            symbol=str(row.symbol),
            direction=str(row.direction),
            status=str(row.status),
            execution_mode=str(row.execution_mode),
            notional=float(row.notional or 0.0),
            quantity=float(row.quantity or 0.0),
            leg_b_quantity=float(row.leg_b_quantity if row.leg_b_quantity is not None else row.quantity or 0.0),
            leg_a_quantity=float(row.leg_a_quantity if row.leg_a_quantity is not None else row.quantity or 0.0),
            open_cost=float(row.open_cost or 0.0),
            fees=float(row.fees or 0.0),
            funding=float(row.funding or 0.0),
            swap=float(row.swap or 0.0),
            realized_pnl=float(row.realized_pnl or 0.0),
            unrealized_pnl=float(row.unrealized_pnl or 0.0),
            trigger_spread=float(row.trigger_spread or 0.0),
            entry_spread=float(row.entry_spread or 0.0),
            entry_threshold=float(row.entry_threshold or 0.0),
            exit_target=float(row.exit_target or 0.0),
            overheat_threshold=float(row.overheat_threshold or 0.0),
            close_reason=str(row.close_reason or ""),
            opened_at=row.opened_at,
            closed_at=row.closed_at,
            source=str(row.source or ""),
        )

    def with_updates(self, **kwargs) -> "HedgeGroupSnapshot":
        """返回一个带指定字段更新的新快照实例。"""
        return replace(self, **kwargs)


@dataclass(frozen=True)
class CloseFillSnapshot:
    """平仓成交快照。"""
    platform: str
    symbol: str
    side: str
    quantity: float
    price: float
    fee: float
    external_order_id: str


@dataclass(frozen=True)
class CloseOrderSnapshot:
    """平仓订单快照。"""
    platform: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    price: float | None
    post_only: bool
    reduce_only: bool
    ttl_seconds: int
    status: str
    external_order_id: str
    average_price: float | None
    error_message: str
    filled_quantity: float
    fee: float
    fills: tuple[CloseFillSnapshot, ...] = ()


@dataclass(frozen=True)
class CloseResultEvent:
    """平仓结果事件，待持久化到数据库。"""
    group_id: int
    status: str
    close_reason: str
    event_type: str
    event_detail: str
    realized_pnl: float | None
    unrealized_pnl: float | None
    fees_delta: float
    closed_at: datetime | None
    orders: tuple[CloseOrderSnapshot, ...] = ()


class HedgePoolStore:
    """线程安全的对冲组内存池。

    使用 ``RLock`` 保护内部字典，支持多线程并发读写。
    主要用途：
    - 自动平仓循环读取 ``snapshot_open_groups()``
    - 执行完成后通过 ``upsert_group()`` 同步状态
    - 平仓结果通过 ``enqueue_close_result()`` 暂存，由持久化循环消费
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._groups: dict[int, HedgeGroupSnapshot] = {}
        self._pending_close_results: Deque[CloseResultEvent] = deque()

    def load_from_db(self, db: Session) -> int:
        """从数据库加载所有活跃对冲组到内存池。

        返回加载的对冲组数量。
        """
        rows = db.query(HedgeGroup).filter(HedgeGroup.status.in_(POOL_GROUP_STATUSES)).all()
        with self._lock:
            current = dict(self._groups)
            snapshots = {}
            for row in rows:
                snapshot = HedgeGroupSnapshot.from_row(row)
                # 保留内存中已有的 unrealized_pnl（可能比数据库更新）
                existing = current.get(snapshot.id)
                if existing and snapshot.status in AUTO_CLOSE_STATUSES and existing.status in AUTO_CLOSE_STATUSES:
                    snapshot = snapshot.with_updates(unrealized_pnl=existing.unrealized_pnl)
                snapshots[snapshot.id] = snapshot
            self._groups = snapshots
        return len(snapshots)

    def snapshot_groups(self) -> list[HedgeGroupSnapshot]:
        """返回所有内存池中对冲组的快照列表（按 symbol, id 排序）。"""
        with self._lock:
            rows = list(self._groups.values())
        return sorted(rows, key=lambda item: (item.symbol, item.id))

    def snapshot_open_groups(self, modes: Iterable[str] | None = None) -> list[HedgeGroupSnapshot]:
        """返回可自动平仓的对冲组列表。

        参数:
            modes: 允许的执行模式列表（如 ``["paper", "live"]``），
                   为 None 或空时返回所有模式。
        """
        allowed_modes = set(modes or [])
        with self._lock:
            rows = [
                group
                for group in self._groups.values()
                if group.status in AUTO_CLOSE_STATUSES and (not allowed_modes or group.execution_mode in allowed_modes)
            ]
        return sorted(rows, key=lambda item: item.opened_at or datetime.min)

    def get(self, group_id: int) -> HedgeGroupSnapshot | None:
        """获取指定对冲组的快照。"""
        with self._lock:
            return self._groups.get(int(group_id))

    def upsert_group(self, group: HedgeGroup | HedgeGroupSnapshot) -> HedgeGroupSnapshot:
        """更新或插入对冲组快照。

        如果对冲组状态不在 ``POOL_GROUP_STATUSES`` 中，则从内存池移除。
        """
        snapshot = group if isinstance(group, HedgeGroupSnapshot) else HedgeGroupSnapshot.from_row(group)
        with self._lock:
            if snapshot.status in POOL_GROUP_STATUSES:
                self._groups[snapshot.id] = snapshot
            else:
                self._groups.pop(snapshot.id, None)
        return snapshot

    def try_mark_closing(self, group_id: int, reason: str = "", estimated_pnl: float | None = None) -> HedgeGroupSnapshot | None:
        """尝试将对冲组标记为 "closing" 状态。

        仅当对冲组处于 ``AUTO_CLOSE_STATUSES`` 状态时才能成功。
        返回更新后的快照，或 None（状态不允许时）。
        """
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current or current.status not in AUTO_CLOSE_STATUSES:
                return None
            updated = current.with_updates(
                status="closing",
                close_reason=reason or current.close_reason,
                unrealized_pnl=current.unrealized_pnl if estimated_pnl is None else float(estimated_pnl),
            )
            self._groups[current.id] = updated
            return updated

    def restore_status(self, snapshot: HedgeGroupSnapshot, status: str | None = None, reason: str = "") -> HedgeGroupSnapshot:
        """恢复对冲组状态（平仓失败时回退）。"""
        restored = snapshot.with_updates(status=status or snapshot.status, close_reason=reason or snapshot.close_reason)
        return self.upsert_group(restored)

    def mark_closed(
        self,
        group_id: int,
        *,
        realized_pnl: float | None = None,
        fees_delta: float = 0.0,
        reason: str = "",
        status: str = "closed",
    ) -> HedgeGroupSnapshot | None:
        """标记对冲组为已平仓。"""
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current:
                return None
            now = utc_now() if status == "closed" else current.closed_at
            updated = current.with_updates(
                status=status,
                closed_at=now,
                realized_pnl=current.realized_pnl if realized_pnl is None else float(realized_pnl),
                unrealized_pnl=0.0 if status == "closed" else current.unrealized_pnl,
                fees=current.fees + float(fees_delta or 0.0),
                close_reason=reason or current.close_reason,
            )
            if updated.status in POOL_GROUP_STATUSES:
                self._groups[updated.id] = updated
            else:
                self._groups.pop(updated.id, None)
            return updated

    def mark_manual_intervention(self, group_id: int, reason: str = "") -> HedgeGroupSnapshot | None:
        """标记对冲组为需要人工介入。"""
        with self._lock:
            current = self._groups.get(int(group_id))
            if not current:
                return None
            updated = current.with_updates(status="manual_intervention", close_reason=reason or current.close_reason)
            self._groups[updated.id] = updated
            return updated

    def remove_closed(self, group_id: int) -> None:
        """从内存池移除已平仓的对冲组。"""
        with self._lock:
            current = self._groups.get(int(group_id))
            if current and current.status == "closed":
                self._groups.pop(int(group_id), None)

    def enqueue_close_result(self, event: CloseResultEvent) -> None:
        """将平仓结果事件加入待持久化队列。"""
        with self._lock:
            self._pending_close_results.append(event)

    def drain_close_results(self, limit: int = 100) -> list[CloseResultEvent]:
        """从待持久化队列中取出最多 limit 个平仓结果事件。"""
        drained: list[CloseResultEvent] = []
        with self._lock:
            while self._pending_close_results and len(drained) < limit:
                drained.append(self._pending_close_results.popleft())
        return drained

    def requeue_close_results(self, events: Iterable[CloseResultEvent]) -> None:
        """将未成功持久化的平仓结果事件重新放回队列头部。"""
        items = list(events)
        if not items:
            return
        with self._lock:
            for event in reversed(items):
                self._pending_close_results.appendleft(event)


# 全局单例内存池
hedge_pool = HedgePoolStore()
