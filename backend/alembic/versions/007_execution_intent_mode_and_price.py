"""为执行意图增加模式，并为执行腿保存限价。

Revision ID: 007_intent_mode_price
Revises: 006_execution_orchestration
"""

import sqlalchemy as sa
from alembic import op

revision = "007_intent_mode_price"
down_revision = "006_execution_orchestration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_intents",
        sa.Column("execution_mode", sa.String(16), server_default="paper", nullable=False),
    )
    op.create_index("ix_execution_intents_mode", "execution_intents", ["execution_mode"])
    op.add_column("execution_legs", sa.Column("limit_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_legs", "limit_price")
    op.drop_index("ix_execution_intents_mode", table_name="execution_intents")
    op.drop_column("execution_intents", "execution_mode")
