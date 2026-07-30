"""统一实际手续费为正数成本口径。

Revision ID: 013_normalize_fees
Revises: 012_portfolio_equity
"""

from alembic import op

revision = "013_normalize_fees"
down_revision = "012_portfolio_equity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # MT5 原始 commission/fee 使用负数表示扣费；账本统一改为正数成本。
    op.execute("UPDATE fills SET fee = -fee WHERE lower(platform) = 'mt5' AND fee < 0")
    op.execute(
        """
        UPDATE venue_orders
        SET commission = -commission
        WHERE commission < 0
          AND execution_leg_id IN (
              SELECT id FROM execution_legs WHERE lower(venue) = 'mt5'
          )
        """
    )
    # 新执行账本存在时以 VenueOrder 为权威；历史组才回退到旧 Fill 汇总。
    op.execute(
        """
        UPDATE hedge_groups
        SET fees = CASE
            WHEN EXISTS (
                SELECT 1
                FROM execution_intents ei
                JOIN execution_legs el ON el.intent_id = ei.id
                JOIN venue_orders vo ON vo.execution_leg_id = el.id
                WHERE ei.hedge_group_id = hedge_groups.id
            ) THEN COALESCE((
                SELECT SUM(vo.commission)
                FROM execution_intents ei
                JOIN execution_legs el ON el.intent_id = ei.id
                JOIN venue_orders vo ON vo.execution_leg_id = el.id
                WHERE ei.hedge_group_id = hedge_groups.id
            ), 0)
            ELSE COALESCE((
                SELECT SUM(f.fee)
                FROM orders o
                JOIN fills f ON f.order_id = o.id
                WHERE o.hedge_group_id = hedge_groups.id
            ), 0)
        END
        """
    )


def downgrade() -> None:
    # 已归一化金额无法可靠判断原 venue 的历史符号约定，不执行破坏性回退。
    pass
