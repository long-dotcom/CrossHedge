"""新增组合权益历史表。

Revision ID: 012_portfolio_equity
Revises: 011_unified_cost_pnl
"""

import sqlalchemy as sa
from alembic import op

revision = "012_portfolio_equity"
down_revision = "011_unified_cost_pnl"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("total_equity", sa.Float(), nullable=True),
        sa.Column("platform_equities", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("data_quality", sa.String(16), nullable=False, server_default="complete"),
        sa.Column("missing_platforms", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_portfolio_equity_snapshots_data_quality", "portfolio_equity_snapshots", ["data_quality"])
    op.create_index("ix_portfolio_equity_created", "portfolio_equity_snapshots", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_portfolio_equity_created", table_name="portfolio_equity_snapshots")
    op.drop_index("ix_portfolio_equity_snapshots_data_quality", table_name="portfolio_equity_snapshots")
    op.drop_table("portfolio_equity_snapshots")
