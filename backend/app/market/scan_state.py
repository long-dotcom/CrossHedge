"""扫描器与 API 通过 Redis 共享当前价差和机会快照。"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from redis.exceptions import WatchError

from app.core.redis_client import redis_client, redis_key
from app.core.time_utils import utc_now


class ScanStateStore:
    """使用单个 Redis JSON 快照保证三组扫描数据同步切换。"""

    def __init__(self, *, key: str | None = None) -> None:
        self._key = key or redis_key("cache", "scan-state")

    def update(
        self,
        spreads: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        direction_spreads: list[dict[str, Any]] | None = None,
    ) -> None:
        state = {
            "spreads": spreads,
            "direction_spreads": direction_spreads if direction_spreads is not None else spreads,
            "opportunities": opportunities,
            "updated_at": utc_now(),
        }
        redis_client().set(self._key, _state_json(state))

    def merge_opportunity_ids(self, ids_by_key: dict[tuple[str, str], int]) -> None:
        if not ids_by_key:
            return

        def merge(state: dict[str, Any]) -> dict[str, Any]:
            for row in state["opportunities"]:
                key = (str(row.get("symbol", "")).upper(), str(row.get("direction", "")))
                if key in ids_by_key:
                    row["id"] = ids_by_key[key]
            state["updated_at"] = utc_now()
            return state
        self._atomic_mutate(merge)

    def remove_symbols(self, symbols: set[str]) -> None:
        if not symbols:
            return
        normalized = {symbol.upper() for symbol in symbols}

        def remove(state: dict[str, Any]) -> dict[str, Any]:
            for name in ("spreads", "direction_spreads", "opportunities"):
                state[name] = [
                    row for row in state[name]
                    if str(row.get("symbol", "")).upper() not in normalized
                ]
            state["updated_at"] = utc_now()
            return state
        self._atomic_mutate(remove)

    def snapshot(self) -> dict[str, Any]:
        raw = redis_client().get(self._key)
        if not raw:
            return {
                "spreads": [], "direction_spreads": [], "opportunities": [],
                "updated_at": None, "ready": False,
            }
        state = _state_from_json(raw)
        state["ready"] = state.get("updated_at") is not None
        return state

    def _atomic_mutate(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        client = redis_client()
        for _ in range(5):
            with client.pipeline() as pipe:
                try:
                    pipe.watch(self._key)
                    raw = pipe.get(self._key)
                    state = _state_from_json(raw) if raw else {
                        "spreads": [], "direction_spreads": [], "opportunities": [], "updated_at": None,
                    }
                    updated = mutator(state)
                    pipe.multi()
                    pipe.set(self._key, _state_json(updated))
                    pipe.execute()
                    return
                except WatchError:
                    continue
        raise RuntimeError("扫描状态 Redis 更新竞争过于频繁")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return {"__type__": "datetime", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__type__": "decimal", "value": str(value)}
    raise TypeError(f"扫描状态包含不可序列化类型: {type(value).__name__}")


def _json_hook(value: dict[str, Any]) -> Any:
    if value.get("__type__") == "datetime":
        return datetime.fromisoformat(value["value"])
    if value.get("__type__") == "decimal":
        return Decimal(value["value"])
    return value


def _state_json(state: dict[str, Any]) -> str:
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _state_from_json(raw: str) -> dict[str, Any]:
    return json.loads(raw, object_hook=_json_hook)


scan_state_store = ScanStateStore()
