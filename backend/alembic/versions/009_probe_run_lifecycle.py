"""补全 ProbeRun 的幂等、Intent 关联和仓位基线。

Revision ID: 009_probe_lifecycle
Revises: 008_persistent_stages
"""

import sqlalchemy as sa
from alembic import op

revision = "009_probe_lifecycle"
down_revision = "008_persistent_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("probe_runs") as batch:
        batch.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("entry_intent_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("exit_intent_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("entry_side", sa.String(16), server_default="BUY", nullable=False))
        batch.add_column(sa.Column("baseline_position_quantity", sa.Float(), server_default="0", nullable=False))
        batch.add_column(sa.Column("final_position_quantity", sa.Float(), nullable=True))
        batch.add_column(sa.Column("flat_confirmed_at", sa.DateTime(), nullable=True))
        batch.create_foreign_key("fk_probe_entry_intent", "execution_intents", ["entry_intent_id"], ["id"])
        batch.create_foreign_key("fk_probe_exit_intent", "execution_intents", ["exit_intent_id"], ["id"])
        batch.create_unique_constraint("uq_probe_runs_idempotency_key", ["idempotency_key"])
    op.create_index("ix_probe_runs_entry_intent", "probe_runs", ["entry_intent_id"])
    op.create_index("ix_probe_runs_exit_intent", "probe_runs", ["exit_intent_id"])


def downgrade() -> None:
    op.drop_index("ix_probe_runs_exit_intent", table_name="probe_runs")
    op.drop_index("ix_probe_runs_entry_intent", table_name="probe_runs")
    with op.batch_alter_table("probe_runs") as batch:
        batch.drop_constraint("uq_probe_runs_idempotency_key", type_="unique")
        batch.drop_constraint("fk_probe_exit_intent", type_="foreignkey")
        batch.drop_constraint("fk_probe_entry_intent", type_="foreignkey")
        batch.drop_column("flat_confirmed_at")
        batch.drop_column("final_position_quantity")
        batch.drop_column("baseline_position_quantity")
        batch.drop_column("entry_side")
        batch.drop_column("exit_intent_id")
        batch.drop_column("entry_intent_id")
        batch.drop_column("idempotency_key")
