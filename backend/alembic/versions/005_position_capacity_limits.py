"""增加品种级与全局开仓容量限制。

Revision ID: 005_position_capacity_limits
Revises: 004_generic_execution_modes
"""

import sqlalchemy as sa
from alembic import op

revision = "005_position_capacity_limits"
down_revision = "004_generic_execution_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("risk_settings", sa.Column("max_total_open_notional", sa.Float(), server_default="10000", nullable=False))
    op.add_column("risk_settings", sa.Column("max_global_open_groups", sa.Integer(), server_default="3", nullable=False))
    op.add_column("risk_settings", sa.Column("max_pending_open_groups", sa.Integer(), server_default="2", nullable=False))
    op.add_column("risk_settings", sa.Column("max_daily_loss", sa.Float(), server_default="0", nullable=False))

    op.add_column("symbol_mappings", sa.Column("target_notional", sa.Float(), server_default="1000", nullable=False))
    op.add_column("symbol_mappings", sa.Column("max_open_notional", sa.Float(), server_default="5000", nullable=False))
    op.add_column("symbol_mappings", sa.Column("max_open_groups", sa.Integer(), server_default="1", nullable=False))
    op.add_column("symbol_mappings", sa.Column("open_cooldown_seconds", sa.Integer(), server_default="30", nullable=False))
    op.add_column("symbol_mappings", sa.Column("max_daily_opens", sa.Integer(), server_default="0", nullable=False))
    op.add_column("symbol_mappings", sa.Column("max_daily_open_notional", sa.Float(), server_default="0", nullable=False))
    op.add_column("symbol_mappings", sa.Column("allow_opposite_direction", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("symbol_mappings", sa.Column("max_holding_minutes", sa.Integer(), server_default="240", nullable=False))

    connection = op.get_bind()
    strategy = connection.execute(sa.text(
        "SELECT default_notional, auto_execute_cooldown_seconds, "
        "auto_execute_max_per_symbol_open_groups, auto_execute_max_global_open_groups, max_holding_minutes "
        "FROM strategy_settings ORDER BY id LIMIT 1"
    )).mappings().first()
    risk = connection.execute(sa.text(
        "SELECT max_symbol_exposure FROM risk_settings ORDER BY id LIMIT 1"
    )).mappings().first()
    if strategy:
        target = max(float(strategy["default_notional"] or 1000), 1.0)
        symbol_limit = max(float((risk or {}).get("max_symbol_exposure") or target), target)
        connection.execute(sa.text(
            "UPDATE symbol_mappings SET target_notional=:target, max_open_notional=:symbol_limit, "
            "max_open_groups=:symbol_groups, open_cooldown_seconds=:cooldown, max_holding_minutes=:holding"
        ), {
            "target": target,
            "symbol_limit": symbol_limit,
            "symbol_groups": max(int(strategy["auto_execute_max_per_symbol_open_groups"] or 1), 1),
            "cooldown": max(int(strategy["auto_execute_cooldown_seconds"] or 0), 0),
            "holding": max(int(strategy["max_holding_minutes"] or 1), 1),
        })
        global_groups = max(int(strategy["auto_execute_max_global_open_groups"] or 1), 1)
        connection.execute(sa.text(
            "UPDATE risk_settings SET max_global_open_groups=:global_groups, max_pending_open_groups=:pending_groups"
        ), {"global_groups": global_groups, "pending_groups": min(2, global_groups)})


def downgrade() -> None:
    for name in (
        "max_holding_minutes", "allow_opposite_direction", "max_daily_open_notional",
        "max_daily_opens", "open_cooldown_seconds", "max_open_groups",
        "max_open_notional", "target_notional",
    ):
        op.drop_column("symbol_mappings", name)
    for name in (
        "max_daily_loss", "max_pending_open_groups", "max_global_open_groups",
        "max_total_open_notional",
    ):
        op.drop_column("risk_settings", name)
