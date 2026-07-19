"""标记混合探针 Paper 执行档案。

Revision ID: 010_hybrid_paper
Revises: 009_probe_lifecycle
"""

import sqlalchemy as sa
from alembic import op

revision = "010_hybrid_paper"
down_revision = "009_probe_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hedge_groups",
        sa.Column("execution_profile", sa.String(32), nullable=False, server_default="legacy_local"),
    )


def downgrade() -> None:
    op.drop_column("hedge_groups", "execution_profile")
