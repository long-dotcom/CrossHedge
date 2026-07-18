"""数据保留策略模块。

提供按 ID 裁剪历史数据的通用工具函数，
用于控制各历史表（如价差快照、日志等）的数据量，
防止数据库无限增长。
"""

import threading
import time

from sqlalchemy.orm import Session


_last_prune_at: dict[str, float] = {}
_prune_lock = threading.Lock()


def prune_table_by_id(db: Session, model, keep: int = 1000, min_interval_seconds: float = 60.0) -> None:
    """按 ID 降序保留最近 N 条记录，删除其余旧数据。

    参数：
        db: 数据库会话
        model: ORM 模型类（必须含有 id 主键）
        keep: 保留的记录数量，默认 1000 条；设为 0 表示不裁剪

    工作原理：
        1. 找到第 keep 条记录的 id 作为截止值
        2. 删除所有 id <= 截止值的记录（即最旧的数据）
    """
    if keep <= 0:
        return
    key = str(getattr(model, "__tablename__", model))
    now = time.monotonic()
    with _prune_lock:
        if now - _last_prune_at.get(key, 0.0) < max(min_interval_seconds, 0.0):
            return
        _last_prune_at[key] = now
    cutoff_id = db.query(model.id).order_by(model.id.desc()).offset(keep).limit(1).scalar()
    if cutoff_id:
        db.query(model).filter(model.id <= cutoff_id).delete(synchronize_session=False)
