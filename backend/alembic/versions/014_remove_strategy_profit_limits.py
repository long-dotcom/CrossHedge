"""移除策略入场、持仓和平仓利润限制字段。

Revision ID: 014_remove_profit_limits
Revises: 013_normalize_fees
"""

import sqlalchemy as sa
from alembic import op

revision = "014_remove_profit_limits"
down_revision = "013_normalize_fees"
branch_labels = None
depends_on = None


_REMOVED_COLUMNS = (
    "min_net_profit",
    "min_annualized_return",
    "min_total_profit",
    "auto_close_unit_profit_buffer",
    "auto_close_min_profit",
    "auto_execute_min_net_profit",
)


def upgrade() -> None:
    # 固定利润模式依赖已移除的阈值，升级后统一切换为统计信号模式。
    op.execute("UPDATE strategy_settings SET signal_mode = 'statistical' WHERE signal_mode <> 'statistical'")
    with op.batch_alter_table("strategy_settings") as batch_op:
        for column_name in _REMOVED_COLUMNS:
            batch_op.drop_column(column_name)


def downgrade() -> None:
    with op.batch_alter_table("strategy_settings") as batch_op:
        batch_op.add_column(sa.Column("min_net_profit", sa.Float(), nullable=False, server_default="5.0"))
        batch_op.add_column(sa.Column("min_annualized_return", sa.Float(), nullable=False, server_default="0.08"))
        batch_op.add_column(sa.Column("min_total_profit", sa.Float(), nullable=False, server_default="0.5"))
        batch_op.add_column(sa.Column("auto_close_unit_profit_buffer", sa.Float(), nullable=False, server_default="0.0"))
        batch_op.add_column(sa.Column("auto_close_min_profit", sa.Float(), nullable=False, server_default="0.0"))
        batch_op.add_column(sa.Column("auto_execute_min_net_profit", sa.Float(), nullable=False, server_default="0.0"))
