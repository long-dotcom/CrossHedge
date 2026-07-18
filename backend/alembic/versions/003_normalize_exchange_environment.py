"""将历史 sandbox 连接环境迁移为 live。

Revision ID: 003_exchange_environment
Revises: 002_backend_performance_indexes
"""

from alembic import op

revision = "003_exchange_environment"
down_revision = "002_backend_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧 sandbox 表示项目 Paper Probe，而非交易所测试网。
    op.execute("UPDATE exchange_credentials SET environment = 'live' WHERE environment = 'sandbox'")


def downgrade() -> None:
    # 无法判断 live 原本是实盘还是旧 sandbox，降级时不回写，避免误改。
    pass
