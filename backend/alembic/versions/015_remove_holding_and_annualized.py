"""移除最大持仓分钟参数与扫描年化收益字段。

Revision ID: 015_remove_holding_limit
Revises: 014_remove_profit_limits
"""

import sqlalchemy as sa
from alembic import op

revision = "015_remove_holding_limit"
down_revision = "014_remove_profit_limits"
branch_labels = None
depends_on = None

_ANNUALIZED_RETURN_TABLES = (
    "spread_current",
    "spread_direction_current",
    "spread_snapshots",
    "arbitrage_opportunities",
)


def upgrade() -> None:
    with op.batch_alter_table("strategy_settings") as batch_op:
        batch_op.drop_column("max_holding_minutes")
    with op.batch_alter_table("symbol_mappings") as batch_op:
        batch_op.drop_column("max_holding_minutes")
    for table_name in _ANNUALIZED_RETURN_TABLES:
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_column("annualized_return")


def downgrade() -> None:
    for table_name in reversed(_ANNUALIZED_RETURN_TABLES):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.add_column(sa.Column("annualized_return", sa.Float(), nullable=False, server_default="0.0"))
    with op.batch_alter_table("strategy_settings") as batch_op:
        batch_op.add_column(sa.Column("max_holding_minutes", sa.Integer(), nullable=False, server_default="240"))
    with op.batch_alter_table("symbol_mappings") as batch_op:
        batch_op.add_column(sa.Column("max_holding_minutes", sa.Integer(), nullable=False, server_default="240"))
