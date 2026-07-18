"""增加高频列表和最新快照查询索引。

Revision ID: 002_backend_performance_indexes
Revises: 001_initial_schema
"""

from alembic import op

revision = "002_backend_performance_indexes"
down_revision = "001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_orders_created_id", "orders", ["created_at", "id"])
    op.create_index("ix_fills_created_id", "fills", ["created_at", "id"])
    op.create_index("ix_alerts_created_id", "alerts", ["created_at", "id"])
    op.create_index("ix_account_snapshots_platform_created", "account_snapshots", ["platform", "created_at", "id"])
    op.create_index("ix_arbitrage_opps_status_profit", "arbitrage_opportunities", ["status", "net_profit"])
    op.create_index("ix_arbitrage_opps_status_updated", "arbitrage_opportunities", ["status", "updated_at"])
    op.create_index("ix_worker_runs_name_created", "worker_runs", ["worker_name", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_runs_name_created", table_name="worker_runs")
    op.drop_index("ix_arbitrage_opps_status_updated", table_name="arbitrage_opportunities")
    op.drop_index("ix_arbitrage_opps_status_profit", table_name="arbitrage_opportunities")
    op.drop_index("ix_account_snapshots_platform_created", table_name="account_snapshots")
    op.drop_index("ix_alerts_created_id", table_name="alerts")
    op.drop_index("ix_fills_created_id", table_name="fills")
    op.drop_index("ix_orders_created_id", table_name="orders")
