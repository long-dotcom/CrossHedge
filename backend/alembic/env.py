"""Alembic 迁移环境配置。

本文件由 Alembic 在每次迁移命令执行时加载，
负责：
1. 配置日志
2. 从应用设置中获取 DATABASE_URL（覆盖 alembic.ini 中的硬编码值）
3. 绑定 ORM 模型的 metadata 作为迁移目标
4. 支持离线模式（生成 SQL 脚本）和在线模式（直接连接数据库执行）
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config.settings import get_settings
from app.db.models import Base

# Alembic 配置对象，用于访问 .ini 文件中的值
config = context.config

# 配置 Python 日志（来自 alembic.ini 的 [loggers] 段）
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从应用设置中获取数据库连接 URL，覆盖 alembic.ini 中的默认值
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# 将 ORM 模型的 metadata 绑定为 Alembic 的目标元数据
# autogenerate 时会对比数据库实际结构与此 metadata 的差异
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连接数据库，直接生成 SQL 迁移脚本。

    使用方式：alembic upgrade head --sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连接数据库并执行迁移。

    使用方式：alembic upgrade head
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
