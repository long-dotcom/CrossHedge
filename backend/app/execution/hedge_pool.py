"""API 与 Worker 共享的对冲组 Redis 快照池。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Callable, Iterable

from redis.exceptions import WatchError
from sqlalchemy.orm import Session

from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now
from app.db.models import HedgeGroup


POOL_GROUP_STATUSES = {"opening", "open", "open_partial", "closing", "manual_intervention"}
AUTO_CLOSE_STATUSES = {"open", "open_partial"}


@dataclass(frozen=True)
class HedgeGroupSnapshot:
    """对冲组的不可变共享快照。"""

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
    estimated_open_fee: float = 0.0
    estimated_close_fee: float = 0.0

    @classmethod
    def from_row(cls, row: HedgeGroup) -> "HedgeGroupSnapshot":
        return cls(
            id=int(row.id), symbol=str(row.symbol), direction=str(row.direction), status=str(row.status),
            execution_mode=str(row.execution_mode), notional=float(row.notional or 0.0),
            quantity=float(row.quantity or 0.0),
            leg_b_quantity=float(row.leg_b_quantity if row.leg_b_quantity is not None else row.quantity or 0.0),
            leg_a_quantity=float(row.leg_a_quantity if row.leg_a_quantity is not None else row.quantity or 0.0),
            open_cost=float(row.open_cost or 0.0), fees=float(row.fees or 0.0),
            estimated_open_fee=float(row.estimated_open_fee or 0.0),
            estimated_close_fee=float(row.estimated_close_fee or 0.0),
            funding=float(row.funding or 0.0), swap=float(row.swap or 0.0),
            realized_pnl=float(row.realized_pnl or 0.0), unrealized_pnl=float(row.unrealized_pnl or 0.0),
            trigger_spread=float(row.trigger_spread or 0.0), entry_spread=float(row.entry_spread or 0.0),
            entry_threshold=float(row.entry_threshold or 0.0), exit_target=float(row.exit_target or 0.0),
            overheat_threshold=float(row.overheat_threshold or 0.0), close_reason=str(row.close_reason or ""),
            opened_at=row.opened_at, closed_at=row.closed_at, source=str(row.source or ""),
        )

    def with_updates(self, **kwargs) -> "HedgeGroupSnapshot":
        return replace(self, **kwargs)


class HedgePoolStore:
    """以 Redis Hash 保存快照，并用 WATCH 保护条件状态转换。"""

    def __init__(self, *, key: str | None = None) -> None:
        self._key = key or redis_key("cache", "hedge-pool")

    def load_from_db(self, db: Session) -> int:
        rows = db.query(HedgeGroup).filter(HedgeGroup.status.in_(POOL_GROUP_STATUSES)).all()
        current = {item.id: item for item in self.snapshot_groups()}
        snapshots: dict[int, HedgeGroupSnapshot] = {}
        for row in rows:
            snapshot = HedgeGroupSnapshot.from_row(row)
            existing = current.get(snapshot.id)
            if existing and snapshot.status in AUTO_CLOSE_STATUSES and existing.status in AUTO_CLOSE_STATUSES:
                snapshot = snapshot.with_updates(unrealized_pnl=existing.unrealized_pnl)
            snapshots[snapshot.id] = snapshot
        pipe = redis_client().pipeline(transaction=True)
        pipe.delete(self._key)
        if snapshots:
            pipe.hset(self._key, mapping={str(key): _snapshot_json(value) for key, value in snapshots.items()})
        pipe.execute()
        return len(snapshots)

    def snapshot_groups(self) -> list[HedgeGroupSnapshot]:
        rows = [_snapshot_from_json(raw) for raw in redis_client().hvals(self._key)]
        return sorted(rows, key=lambda item: (item.symbol, item.id))

    def snapshot_open_groups(self, modes: Iterable[str] | None = None) -> list[HedgeGroupSnapshot]:
        allowed_modes = set(modes or [])
        rows = [
            group for group in self.snapshot_groups()
            if group.status in AUTO_CLOSE_STATUSES and (not allowed_modes or group.execution_mode in allowed_modes)
        ]
        return sorted(rows, key=lambda item: item.opened_at or datetime.min)

    def get(self, group_id: int) -> HedgeGroupSnapshot | None:
        raw = redis_client().hget(self._key, str(int(group_id)))
        return _snapshot_from_json(raw) if raw else None

    def upsert_group(self, group: HedgeGroup | HedgeGroupSnapshot) -> HedgeGroupSnapshot:
        snapshot = group if isinstance(group, HedgeGroupSnapshot) else HedgeGroupSnapshot.from_row(group)
        if snapshot.status in POOL_GROUP_STATUSES:
            redis_client().hset(self._key, str(snapshot.id), _snapshot_json(snapshot))
        else:
            redis_client().hdel(self._key, str(snapshot.id))
        return snapshot

    def try_mark_closing(self, group_id: int, reason: str = "", estimated_pnl: float | None = None) -> HedgeGroupSnapshot | None:
        def update(current: HedgeGroupSnapshot | None) -> HedgeGroupSnapshot | None:
            if not current or current.status not in AUTO_CLOSE_STATUSES:
                return None
            return current.with_updates(
                status="closing", close_reason=reason or current.close_reason,
                unrealized_pnl=current.unrealized_pnl if estimated_pnl is None else float(estimated_pnl),
            )
        return self._atomic_update(group_id, update)

    def restore_status(self, snapshot: HedgeGroupSnapshot, status: str | None = None, reason: str = "") -> HedgeGroupSnapshot:
        return self.upsert_group(snapshot.with_updates(status=status or snapshot.status, close_reason=reason or snapshot.close_reason))

    def mark_closed(
        self, group_id: int, *, realized_pnl: float | None = None, fees_delta: float = 0.0,
        reason: str = "", status: str = "closed",
    ) -> HedgeGroupSnapshot | None:
        def update(current: HedgeGroupSnapshot | None) -> HedgeGroupSnapshot | None:
            if not current:
                return None
            return current.with_updates(
                status=status, closed_at=utc_now() if status == "closed" else current.closed_at,
                realized_pnl=current.realized_pnl if realized_pnl is None else float(realized_pnl),
                unrealized_pnl=0.0 if status == "closed" else current.unrealized_pnl,
                fees=current.fees + float(fees_delta or 0.0), close_reason=reason or current.close_reason,
            )
        return self._atomic_update(group_id, update)

    def mark_manual_intervention(self, group_id: int, reason: str = "") -> HedgeGroupSnapshot | None:
        def update(current: HedgeGroupSnapshot | None) -> HedgeGroupSnapshot | None:
            if not current:
                return None
            return current.with_updates(status="manual_intervention", close_reason=reason or current.close_reason)
        return self._atomic_update(group_id, update)

    def remove_closed(self, group_id: int) -> None:
        current = self.get(group_id)
        if current and current.status == "closed":
            redis_client().hdel(self._key, str(int(group_id)))

    def _atomic_update(
        self, group_id: int,
        updater: Callable[[HedgeGroupSnapshot | None], HedgeGroupSnapshot | None],
    ) -> HedgeGroupSnapshot | None:
        client = redis_client()
        field = str(int(group_id))
        for _ in range(5):
            with client.pipeline() as pipe:
                try:
                    pipe.watch(self._key)
                    raw = pipe.hget(self._key, field)
                    updated = updater(_snapshot_from_json(raw) if raw else None)
                    if updated is None:
                        pipe.unwatch()
                        return None
                    pipe.multi()
                    if updated.status in POOL_GROUP_STATUSES:
                        pipe.hset(self._key, field, _snapshot_json(updated))
                    else:
                        pipe.hdel(self._key, field)
                    pipe.execute()
                    return updated
                except WatchError:
                    continue
        raise RuntimeError(f"对冲组 Redis 状态竞争过于频繁: {group_id}")


def _snapshot_json(snapshot: HedgeGroupSnapshot) -> str:
    data = asdict(snapshot)
    for field in ("opened_at", "closed_at"):
        data[field] = data[field].isoformat() if data[field] else None
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _snapshot_from_json(raw: str) -> HedgeGroupSnapshot:
    data = json.loads(raw)
    for field in ("opened_at", "closed_at"):
        if data.get(field):
            data[field] = datetime.fromisoformat(data[field])
    return HedgeGroupSnapshot(**data)


hedge_pool = HedgePoolStore()
