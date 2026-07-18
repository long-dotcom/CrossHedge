"""增加下一代执行意图、事件、仓位快照和 Probe 基础表。

Revision ID: 006_execution_orchestration
Revises: 005_position_capacity_limits
"""

import sqlalchemy as sa
from alembic import op

revision = "006_execution_orchestration"
down_revision = "005_position_capacity_limits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "execution_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hedge_group_id", sa.Integer(), sa.ForeignKey("hedge_groups.id"), nullable=True),
        sa.Column("intent_type", sa.String(32), nullable=False),
        sa.Column("requested_by", sa.String(64), server_default="system", nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), server_default="CREATED", nullable=False),
        sa.Column("expected_group_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_execution_intents_idempotency_key"),
    )
    op.create_index("ix_execution_intents_group", "execution_intents", ["hedge_group_id"])
    op.create_index("ix_execution_intents_type", "execution_intents", ["intent_type"])
    op.create_index("ix_execution_intents_status", "execution_intents", ["status"])

    op.create_table(
        "execution_legs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("intent_id", sa.Integer(), sa.ForeignKey("execution_intents.id"), nullable=False),
        sa.Column("leg_key", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.String(128), server_default="", nullable=False),
        sa.Column("venue_symbol", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("position_side", sa.String(16), server_default="NET", nullable=False),
        sa.Column("order_side", sa.String(16), nullable=False),
        sa.Column("strategy_quantity", sa.Float(), nullable=False),
        sa.Column("venue_order_quantity", sa.Float(), nullable=False),
        sa.Column("target_position_quantity_before", sa.Float(), nullable=True),
        sa.Column("target_position_quantity_after", sa.Float(), nullable=True),
        sa.Column("order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("time_in_force", sa.String(16), server_default="GTC", nullable=False),
        sa.Column("post_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("venue_reduce_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(32), server_default="PLANNED", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("intent_id", "leg_key", name="uq_execution_leg_intent_key"),
    )
    op.create_index("ix_execution_legs_intent", "execution_legs", ["intent_id"])
    op.create_index("ix_execution_legs_venue", "execution_legs", ["venue"])
    op.create_index("ix_execution_legs_status", "execution_legs", ["status"])

    op.create_table(
        "venue_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_leg_id", sa.Integer(), sa.ForeignKey("execution_legs.id"), nullable=False),
        sa.Column("legacy_order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("client_order_id", sa.String(128), nullable=False),
        sa.Column("venue_order_id", sa.String(128), server_default="", nullable=False),
        sa.Column("command_id", sa.String(64), server_default="", nullable=False),
        sa.Column("correlation_id", sa.String(64), server_default="", nullable=False),
        sa.Column("position_id", sa.String(160), server_default="", nullable=False),
        sa.Column("status", sa.String(32), server_default="INITIALIZED", nullable=False),
        sa.Column("requested_quantity", sa.Float(), nullable=False),
        sa.Column("filled_quantity", sa.Float(), server_default="0", nullable=False),
        sa.Column("remaining_quantity", sa.Float(), nullable=False),
        sa.Column("average_price", sa.Float(), nullable=True),
        sa.Column("commission", sa.Float(), server_default="0", nullable=False),
        sa.Column("reconciliation_state", sa.String(32), server_default="LOCAL", nullable=False),
        sa.Column("raw_last_report", sa.Text(), server_default="", nullable=False),
        sa.Column("last_event_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_venue_orders_client_order_id"),
    )
    for name, columns in (
        ("ix_venue_orders_leg", ["execution_leg_id"]),
        ("ix_venue_orders_legacy", ["legacy_order_id"]),
        ("ix_venue_orders_venue_order_id", ["venue_order_id"]),
        ("ix_venue_orders_correlation", ["correlation_id"]),
        ("ix_venue_orders_position", ["position_id"]),
        ("ix_venue_orders_status", ["status"]),
    ):
        op.create_index(name, "venue_orders", columns)

    op.create_table(
        "execution_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.Integer(), sa.ForeignKey("execution_intents.id"), nullable=True),
        sa.Column("execution_leg_id", sa.Integer(), sa.ForeignKey("execution_legs.id"), nullable=True),
        sa.Column("venue_order_id_ref", sa.Integer(), sa.ForeignKey("venue_orders.id"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("client_order_id", sa.String(128), server_default="", nullable=False),
        sa.Column("venue_order_id", sa.String(128), server_default="", nullable=False),
        sa.Column("ts_event", sa.DateTime(), nullable=True),
        sa.Column("ts_init", sa.DateTime(), nullable=True),
        sa.Column("reconciliation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_execution_events_event_id"),
    )
    for name, columns in (
        ("ix_execution_events_intent", ["intent_id"]),
        ("ix_execution_events_leg", ["execution_leg_id"]),
        ("ix_execution_events_order", ["venue_order_id_ref"]),
        ("ix_execution_events_type", ["event_type"]),
        ("ix_execution_events_client_order", ["client_order_id"]),
        ("ix_execution_events_venue_order", ["venue_order_id"]),
    ):
        op.create_index(name, "execution_events", columns)

    op.create_table(
        "execution_position_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(128), server_default="", nullable=False),
        sa.Column("instrument_id", sa.String(128), nullable=False),
        sa.Column("position_id", sa.String(160), server_default="", nullable=False),
        sa.Column("position_side", sa.String(16), server_default="NET", nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("mark_price", sa.Float(), nullable=True),
        sa.Column("source_event_id", sa.String(64), server_default="", nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("is_fresh", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("venue", "account_id", "instrument_id", "position_id", name="uq_exec_position_identity"),
    )
    op.create_index("ix_exec_position_venue", "execution_position_snapshots", ["venue"])
    op.create_index("ix_exec_position_instrument", "execution_position_snapshots", ["instrument_id"])
    op.create_index("ix_exec_position_observed", "execution_position_snapshots", ["observed_at"])

    op.create_table(
        "probe_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hedge_group_id", sa.Integer(), sa.ForeignKey("hedge_groups.id"), nullable=True),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("venue", sa.String(32), nullable=False),
        sa.Column("instrument_id", sa.String(128), nullable=False),
        sa.Column("position_side", sa.String(16), server_default="NET", nullable=False),
        sa.Column("probe_quantity", sa.Float(), nullable=False),
        sa.Column("open_venue_order_id", sa.Integer(), sa.ForeignKey("venue_orders.id"), nullable=True),
        sa.Column("close_venue_order_id", sa.Integer(), sa.ForeignKey("venue_orders.id"), nullable=True),
        sa.Column("open_fill_price", sa.Float(), nullable=True),
        sa.Column("close_fill_price", sa.Float(), nullable=True),
        sa.Column("residual_quantity", sa.Float(), server_default="0", nullable=False),
        sa.Column("status", sa.String(32), server_default="CREATED", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_probe_runs_group", "probe_runs", ["hedge_group_id"])
    op.create_index("ix_probe_runs_venue", "probe_runs", ["venue"])
    op.create_index("ix_probe_runs_instrument", "probe_runs", ["instrument_id"])
    op.create_index("ix_probe_runs_status", "probe_runs", ["status"])

    op.create_table(
        "execution_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("intent_id", sa.Integer(), sa.ForeignKey("execution_intents.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.Text(), server_default="{}", nullable=False),
        sa.Column("status", sa.String(32), server_default="PENDING", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_execution_outbox_intent", "execution_outbox", ["intent_id"])
    op.create_index("ix_execution_outbox_status", "execution_outbox", ["status"])
    op.create_index("ix_execution_outbox_available", "execution_outbox", ["available_at"])


def downgrade() -> None:
    op.drop_table("execution_outbox")
    op.drop_table("probe_runs")
    op.drop_table("execution_position_snapshots")
    op.drop_table("execution_events")
    op.drop_table("venue_orders")
    op.drop_table("execution_legs")
    op.drop_table("execution_intents")
