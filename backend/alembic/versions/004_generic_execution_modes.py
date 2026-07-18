"""增加与交易所无关的双腿执行模式字段。

Revision ID: 004_generic_execution_modes
Revises: 003_exchange_environment
"""

import sqlalchemy as sa
from alembic import op

revision = "004_generic_execution_modes"
down_revision = "003_exchange_environment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("symbol_mappings", sa.Column("maker_leg", sa.String(1), server_default="a", nullable=False))
    op.add_column("symbol_mappings", sa.Column("maker_offset_bps", sa.Float(), server_default="1.0", nullable=False))
    op.add_column("symbol_mappings", sa.Column("maker_order_ttl_seconds", sa.Integer(), server_default="3", nullable=False))
    op.add_column("symbol_mappings", sa.Column("maker_unfilled_action", sa.String(32), server_default="cancel", nullable=False))
    op.add_column("symbol_mappings", sa.Column("leg_a_close_order_type", sa.String(16), server_default="market", nullable=False))
    op.add_column("symbol_mappings", sa.Column("leg_b_close_order_type", sa.String(16), server_default="market", nullable=False))
    op.execute("UPDATE symbol_mappings SET execution_style = 'simultaneous_market' WHERE execution_style = 'taker_taker'")
    op.execute("UPDATE symbol_mappings SET execution_style = 'maker_then_market', maker_leg = 'a' WHERE execution_style = 'hyper_maker_mt5_taker'")
    op.execute("UPDATE symbol_mappings SET maker_offset_bps = hl_maker_offset_bps, maker_order_ttl_seconds = hl_order_ttl_seconds")
    op.execute("UPDATE symbol_mappings SET maker_unfilled_action = CASE WHEN hl_unfilled_action = 'taker_fallback' THEN 'market_fallback' ELSE 'cancel' END")
    op.execute("UPDATE symbol_mappings SET leg_a_close_order_type = hl_close_order_type, leg_b_close_order_type = mt5_close_order_type")


def downgrade() -> None:
    op.execute("UPDATE symbol_mappings SET execution_style = 'taker_taker' WHERE execution_style = 'simultaneous_market'")
    op.execute("UPDATE symbol_mappings SET execution_style = 'hyper_maker_mt5_taker' WHERE execution_style = 'maker_then_market' AND maker_leg = 'a'")
    for name in (
        "leg_b_close_order_type", "leg_a_close_order_type", "maker_unfilled_action",
        "maker_order_ttl_seconds", "maker_offset_bps", "maker_leg",
    ):
        op.drop_column("symbol_mappings", name)
