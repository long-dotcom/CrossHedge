"""为持久化 Maker/补偿状态机增加执行阶段字段。

Revision ID: 008_persistent_stages
Revises: 007_intent_mode_price
"""

import sqlalchemy as sa
from alembic import op

revision = "008_persistent_stages"
down_revision = "007_intent_mode_price"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_intents",
        sa.Column("execution_style", sa.String(32), server_default="simultaneous_market", nullable=False),
    )
    op.create_index("ix_execution_intents_style", "execution_intents", ["execution_style"])
    with op.batch_alter_table("execution_legs") as batch:
        batch.add_column(sa.Column("role", sa.String(32), server_default="PRIMARY", nullable=False))
        batch.add_column(sa.Column("parent_leg_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("sequence", sa.Integer(), server_default="0", nullable=False))
        batch.create_foreign_key(
            "fk_execution_legs_parent",
            "execution_legs",
            ["parent_leg_id"],
            ["id"],
        )
    op.create_index("ix_execution_legs_parent", "execution_legs", ["parent_leg_id"])
    op.create_index("ix_execution_legs_role", "execution_legs", ["role"])


def downgrade() -> None:
    op.drop_index("ix_execution_legs_role", table_name="execution_legs")
    op.drop_index("ix_execution_legs_parent", table_name="execution_legs")
    with op.batch_alter_table("execution_legs") as batch:
        batch.drop_constraint("fk_execution_legs_parent", type_="foreignkey")
        batch.drop_column("sequence")
        batch.drop_column("parent_leg_id")
        batch.drop_column("role")
    op.drop_index("ix_execution_intents_style", table_name="execution_intents")
    op.drop_column("execution_intents", "execution_style")
