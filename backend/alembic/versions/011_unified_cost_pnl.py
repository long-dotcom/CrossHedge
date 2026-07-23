"""统一成本与 PnL 口径字段。

Revision ID: 011_unified_cost_pnl
Revises: 010_hybrid_paper
"""

import sqlalchemy as sa
from alembic import op

revision = "011_unified_cost_pnl"
down_revision = "010_hybrid_paper"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("arbitrage_opportunities", sa.Column("spread_cost", sa.Float(), nullable=False, server_default="0"))
    op.add_column("arbitrage_opportunities", sa.Column("estimated_open_fee", sa.Float(), nullable=False, server_default="0"))
    op.add_column("arbitrage_opportunities", sa.Column("estimated_close_fee", sa.Float(), nullable=False, server_default="0"))
    op.add_column("hedge_groups", sa.Column("estimated_open_fee", sa.Float(), nullable=False, server_default="0"))
    op.add_column("hedge_groups", sa.Column("estimated_close_fee", sa.Float(), nullable=False, server_default="0"))
    # 旧机会只有预计往返手续费，无法准确拆分时按一半回填，保持总额不变。
    op.execute("UPDATE arbitrage_opportunities SET estimated_open_fee = total_cost / 2, estimated_close_fee = total_cost / 2")
    op.execute("UPDATE hedge_groups SET estimated_open_fee = open_cost / 2, estimated_close_fee = open_cost / 2")


def downgrade() -> None:
    op.drop_column("hedge_groups", "estimated_close_fee")
    op.drop_column("hedge_groups", "estimated_open_fee")
    op.drop_column("arbitrage_opportunities", "estimated_close_fee")
    op.drop_column("arbitrage_opportunities", "estimated_open_fee")
    op.drop_column("arbitrage_opportunities", "spread_cost")
