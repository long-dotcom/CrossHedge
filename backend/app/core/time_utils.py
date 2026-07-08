"""
时间工具模块

统一 UTC 时间获取逻辑，消除源项目中大量重复的
``datetime.now(timezone.utc).replace(tzinfo=None)`` 模式（30+ 处）。

返回 naive datetime（无时区信息），兼容 SQLite 和 PostgreSQL 存储。

使用方式::

    from app.core.time_utils import utc_now

    now = utc_now()
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """返回当前 UTC 时间的 naive datetime（无时区信息）。

    等价于 ``datetime.now(timezone.utc).replace(tzinfo=None)``，
    但语义更清晰、调用更简洁。

    返回:
        当前 UTC 时间，不含 tzinfo，可直接存入 SQLite / PostgreSQL。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
