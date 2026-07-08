"""
数据库会话上下文管理器模块

封装 SQLAlchemy Session 的获取、提交、回滚和关闭逻辑，
消除源项目中大量重复的 ``SessionLocal() + try/except/finally/close`` 模式：
- ``workers/scheduler.py`` 中每个定时任务的样板代码
- ``db/session.py`` 的 ``get_db`` 生成器

使用方式::

    from app.core.db_session import db_session

    with db_session() as db:
        db.query(SomeModel).all()
        # 正常退出自动 commit
        # 异常时自动 rollback
        # finally 中自动 close session
"""

from __future__ import annotations

import contextlib
from typing import Generator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


@contextlib.contextmanager
def db_session() -> Generator[Session, None, None]:
    """数据库会话上下文管理器。

    自动管理 Session 生命周期：
    - 正常退出时 ``commit()``
    - 异常时 ``rollback()``
    - 最终 ``close()`` 释放连接

    产出:
        SQLAlchemy ``Session`` 实例。

    使用示例::

        from app.core.db_session import db_session

        with db_session() as db:
            users = db.query(User).all()
            # 无需手动 commit / close
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
