"""
Worker 执行模板模块

封装定时任务的标准执行流程，消除源项目中大量重复的
``try/except + WorkerRun + SystemLog + prune + commit`` 样板代码：
- ``workers/scheduler.py`` 中每个 job 函数的模式
- ``execution/auto_executor.py``、``execution/carry_costs.py``
- ``execution/persistence.py`` 等

使用方式::

    from app.core.db_session import db_session
    from app.core.worker_runner import run_worker
    from app.db.models import WorkerRun, SystemLog

    with db_session() as db:
        result = run_worker(
            db,
            "carry_cost_sync",
            my_task_func,
            prune_models=[WorkerRun, SystemLog],
        )
"""

from __future__ import annotations

import time
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.time_utils import utc_now

logger = get_logger(__name__)


def run_worker(
    db: Session,
    worker_name: str,
    func: Callable[[Session], Any],
    *,
    prune_models: list | None = None,
) -> Any:
    """执行一个 Worker 任务，自动记录运行日志和异常信息。

    标准流程：
    1. 记录开始时间
    2. 执行 ``func(db)``
    3. 成功：写入 ``WorkerRun(status="success")``
    4. 失败：写入 ``WorkerRun(status="failed")`` + ``SystemLog``
    5. 执行 ``prune_table_by_id``（如果指定了 *prune_models*）
    6. ``db.commit()``
    7. 返回 ``func`` 的结果

    参数:
        db: SQLAlchemy 数据库会话。
        worker_name: Worker 名称，用于 ``WorkerRun.worker_name`` 和日志。
        func: 实际执行的任务函数，签名为 ``func(db) -> Any``。
        prune_models: 需要在完成后执行 ``prune_table_by_id`` 的 ORM 模型列表，
            例如 ``[WorkerRun, SystemLog]``。

    返回:
        ``func(db)`` 的返回值。

    异常:
        重新抛出 ``func`` 执行过程中的原始异常，以便上层处理。

    使用示例::

        from app.db.models import WorkerRun, SystemLog

        with db_session() as db:
            result = run_worker(
                db,
                "spread_scanner",
                lambda db: run_scan(db),
                prune_models=[WorkerRun, SystemLog],
            )
    """
    # 延迟导入，避免循环依赖（db.models 和 db.retention 依赖 db.session）
    from app.db.models import SystemLog, WorkerRun
    from app.db.retention import prune_table_by_id

    started = time.perf_counter()
    try:
        result = func(db)
        duration_ms = int((time.perf_counter() - started) * 1000)

        # 成功：记录 WorkerRun
        db.add(WorkerRun(
            worker_name=worker_name,
            status="success",
            duration_ms=duration_ms,
        ))
        logger.debug("Worker 成功: name={}, duration={}ms", worker_name, duration_ms)

        # 执行 prune
        if prune_models:
            for model in prune_models:
                prune_table_by_id(db, model)

        db.commit()
        return result

    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)

        # 失败：记录 WorkerRun + SystemLog
        db.rollback()
        db.add(WorkerRun(
            worker_name=worker_name,
            status="failed",
            duration_ms=duration_ms,
            error_message=str(exc),
        ))
        db.add(SystemLog(
            level="error",
            category=worker_name,
            message=f"Worker 执行失败: {worker_name}",
            context=str(exc),
        ))

        # 即使失败也尝试 prune，防止表膨胀
        if prune_models:
            for model in prune_models:
                prune_table_by_id(db, model)

        db.commit()
        logger.exception("Worker 失败: name={}, error={}", worker_name, exc)
        raise
