"""
扫描状态存储模块
=================

提供线程安全的扫描结果状态存储，用于在扫描器和 API 之间共享数据：

- 价差数据（spreads）
- 方向价差数据（direction_spreads）
- 套利机会列表（opportunities）
- 最后更新时间戳

所有操作均通过内部锁保证线程安全。
"""

from __future__ import annotations

from copy import deepcopy
from threading import Lock
from typing import Any

from app.core.logging import get_logger
from app.core.time_utils import utc_now

logger = get_logger(__name__)


class ScanStateStore:
    """扫描状态存储。

    线程安全地存储和更新扫描器产出的价差数据和套利机会。
    API 层通过 :meth:`snapshot` 获取当前状态的深拷贝。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        # 全品种价差数据
        self._spreads: list[dict[str, Any]] = []
        # 按方向分类的价差数据
        self._direction_spreads: list[dict[str, Any]] = []
        # 套利机会列表
        self._opportunities: list[dict[str, Any]] = []
        # 最后更新时间戳
        self._updated_at = None

    def update(
        self,
        spreads: list[dict[str, Any]],
        opportunities: list[dict[str, Any]],
        direction_spreads: list[dict[str, Any]] | None = None,
    ) -> None:
        """全量更新扫描结果。

        参数:
            spreads: 全品种价差数据列表。
            opportunities: 套利机会列表。
            direction_spreads: 按方向分类的价差数据（为 None 时使用 spreads）。
        """
        with self._lock:
            self._spreads = deepcopy(spreads)
            self._direction_spreads = deepcopy(direction_spreads if direction_spreads is not None else spreads)
            self._opportunities = deepcopy(opportunities)
            self._updated_at = utc_now()

    def merge_opportunity_ids(self, ids_by_key: dict[tuple[str, str], int]) -> None:
        """将数据库 ID 合并到套利机会中。

        按 ``(symbol, direction)`` 键匹配，将对应的数据库 ID 写入机会记录。

        参数:
            ids_by_key: ``{(symbol_upper, direction): id}`` 映射字典。
        """
        if not ids_by_key:
            return
        with self._lock:
            for row in self._opportunities:
                key = (str(row.get("symbol", "")).upper(), str(row.get("direction", "")))
                if key in ids_by_key:
                    row["id"] = ids_by_key[key]
            self._updated_at = utc_now()

    def remove_symbols(self, symbols: set[str]) -> None:
        """从存储中移除指定品种的所有数据。

        参数:
            symbols: 要移除的品种名称集合（不区分大小写）。
        """
        if not symbols:
            return
        normalized = {symbol.upper() for symbol in symbols}
        with self._lock:
            self._spreads = [
                row for row in self._spreads
                if str(row.get("symbol", "")).upper() not in normalized
            ]
            self._direction_spreads = [
                row for row in self._direction_spreads
                if str(row.get("symbol", "")).upper() not in normalized
            ]
            self._opportunities = [
                row for row in self._opportunities
                if str(row.get("symbol", "")).upper() not in normalized
            ]
            self._updated_at = utc_now()

    def snapshot(self) -> dict[str, Any]:
        """获取当前扫描状态的深拷贝快照。

        返回:
            包含以下字段的字典：
            - ``spreads``: 全品种价差数据
            - ``direction_spreads``: 方向价差数据
            - ``opportunities``: 套利机会列表
            - ``updated_at``: 最后更新时间
            - ``ready``: 是否已有数据
        """
        with self._lock:
            return {
                "spreads": deepcopy(self._spreads),
                "direction_spreads": deepcopy(self._direction_spreads),
                "opportunities": deepcopy(self._opportunities),
                "updated_at": self._updated_at,
                "ready": self._updated_at is not None,
            }


# 全局扫描状态存储单例
scan_state_store = ScanStateStore()
