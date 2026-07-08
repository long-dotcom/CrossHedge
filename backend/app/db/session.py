"""数据库会话管理模块。

负责创建 SQLAlchemy 数据库引擎、连接池配置以及会话工厂。
支持 SQLite（开发/测试）和 PostgreSQL（生产）两种模式，
根据配置中的 DATABASE_URL 自动选择对应的连接参数。

主要导出：
- engine: SQLAlchemy 引擎实例
- SessionLocal: 会话工厂，用于创建数据库会话
- get_db(): FastAPI 依赖注入用的会话生成器
- IS_SQLITE / IS_POSTGRESQL: 数据库类型标识常量
"""

from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import ROOT_DIR, get_settings


settings = get_settings()
database_url = settings.database.url

# 根据 URL 前缀判断数据库类型，用于后续差异化配置
IS_SQLITE = database_url.startswith("sqlite")
IS_POSTGRESQL = database_url.startswith("postgresql")

if IS_SQLITE:
    # SQLite：解析相对路径为绝对路径，确保工作目录无关
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        path = Path(db_path)
        if not path.is_absolute():
            path = ROOT_DIR / path
            database_url = f"sqlite:///{path.as_posix()}"
            db_path = str(path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # SQLite 连接参数：允许跨线程使用，设置超时避免锁等待
    connect_args = {"check_same_thread": False, "timeout": 30}
    engine_kwargs = {"connect_args": connect_args, "future": True}
elif IS_POSTGRESQL:
    # PostgreSQL：使用连接池，启用 pre_ping 检测断线重连
    connect_args = {}
    engine_kwargs = {
        "connect_args": connect_args,
        "future": True,
        "pool_size": settings.database.pool_size,
        "max_overflow": settings.database.max_overflow,
        "pool_recycle": settings.database.pool_recycle,
        "pool_pre_ping": True,
    }
else:
    connect_args = {}
    engine_kwargs = {"connect_args": connect_args, "future": True}

engine = create_engine(database_url, **engine_kwargs)


# SQLite 性能优化：设置 WAL 模式与忙等待超时
if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """在 SQLite 连接建立时设置 PRAGMA 参数，提升并发性能。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


# 会话工厂：禁用自动 flush 和 commit，由调用方显式控制事务
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的数据库会话生成器。

    使用方式（在路由函数中）：
        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...

    会话在请求结束后自动关闭，确保连接正确归还给连接池。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
