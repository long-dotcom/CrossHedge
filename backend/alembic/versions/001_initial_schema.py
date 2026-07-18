"""初始数据库 Schema 创建

Revision ID: 001_initial_schema
Revises: (无)
Create Date: 2026-07-03

本迁移脚本基于 app/db/models.py 中的 ORM 定义手动编写，
创建系统所需的全部数据库表及复合索引。
"""

from alembic import op
import sqlalchemy as sa

revision = "001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 用户表 ──────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), unique=True, nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), server_default="admin", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # ── 审计日志表 ──────────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource", sa.String(128), server_default="", nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("ip_address", sa.String(64), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 系统配置表（键值对）─────────────────────────────────────────────────
    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 交易所凭据表 ────────────────────────────────────────────────────────
    op.create_table(
        "exchange_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(32), unique=True, nullable=False),
        sa.Column("display_name", sa.String(64), server_default="", nullable=False),
        sa.Column("environment", sa.String(32), server_default="live", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), server_default="", nullable=False),
        sa.Column("credentials_fingerprint", sa.String(64), server_default="", nullable=False),
        sa.Column("last_test_status", sa.String(32), server_default="untested", nullable=False),
        sa.Column("last_test_message", sa.Text(), server_default="", nullable=False),
        sa.Column("last_tested_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_exchange_credentials_venue", "exchange_credentials", ["venue"], unique=True)

    # ── 策略参数表 ──────────────────────────────────────────────────────────
    op.create_table(
        "strategy_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("min_net_profit", sa.Float(), server_default="5.0", nullable=False),
        sa.Column("min_annualized_return", sa.Float(), server_default="0.08", nullable=False),
        sa.Column("signal_mode", sa.String(32), server_default="statistical", nullable=False),
        sa.Column("statistical_lookback_range", sa.String(16), server_default="1h", nullable=False),
        sa.Column("statistical_min_samples", sa.Integer(), server_default="200", nullable=False),
        sa.Column("reachable_entry_percentile", sa.Float(), server_default="0.75", nullable=False),
        sa.Column("reachable_entry_zscore", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("cost_guard_percentile", sa.Float(), server_default="0.90", nullable=False),
        sa.Column("min_unit_edge", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("min_total_profit", sa.Float(), server_default="0.5", nullable=False),
        sa.Column("auto_close_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("auto_close_live_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("exit_target_percentile", sa.Float(), server_default="0.25", nullable=False),
        sa.Column("auto_close_unit_profit_buffer", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("auto_close_min_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("default_notional", sa.Float(), server_default="1000.0", nullable=False),
        sa.Column("max_holding_minutes", sa.Integer(), server_default="240", nullable=False),
        sa.Column("execution_mode", sa.String(32), server_default="paper", nullable=False),
        sa.Column("paper_use_live_account_risk", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("auto_execute_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("auto_execute_paper_only", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("auto_execute_min_hold_ms", sa.Integer(), server_default="300", nullable=False),
        sa.Column("auto_execute_confirm_ticks", sa.Integer(), server_default="2", nullable=False),
        sa.Column("auto_execute_cooldown_seconds", sa.Integer(), server_default="30", nullable=False),
        sa.Column("auto_execute_max_per_symbol_open_groups", sa.Integer(), server_default="1", nullable=False),
        sa.Column("auto_execute_max_global_open_groups", sa.Integer(), server_default="3", nullable=False),
        sa.Column("auto_execute_min_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("paper_decision_delay_ms_min", sa.Integer(), server_default="50", nullable=False),
        sa.Column("paper_decision_delay_ms_max", sa.Integer(), server_default="200", nullable=False),
        sa.Column("paper_leg_a_latency_ms_min", sa.Integer(), server_default="80", nullable=False),
        sa.Column("paper_leg_a_latency_ms_max", sa.Integer(), server_default="200", nullable=False),
        sa.Column("paper_leg_b_latency_ms_min", sa.Integer(), server_default="120", nullable=False),
        sa.Column("paper_leg_b_latency_ms_max", sa.Integer(), server_default="350", nullable=False),
        sa.Column("cb_cooldown_seconds", sa.Float(), server_default="3.0", nullable=False),
        sa.Column("cb_initial_threshold", sa.Float(), server_default="0.75", nullable=False),
        sa.Column("cb_baseline_multiplier", sa.Float(), server_default="2.0", nullable=False),
        sa.Column("cb_min_baseline_samples", sa.Integer(), server_default="50", nullable=False),
        sa.Column("cb_detection_seconds", sa.Float(), server_default="5.0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 风控参数表 ──────────────────────────────────────────────────────────
    op.create_table(
        "risk_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(32), server_default="normal", nullable=False),
        sa.Column("max_order_notional", sa.Float(), server_default="2000.0", nullable=False),
        sa.Column("max_symbol_exposure", sa.Float(), server_default="5000.0", nullable=False),
        sa.Column("max_total_leverage", sa.Float(), server_default="2.0", nullable=False),
        sa.Column("max_new_margin_fraction", sa.Float(), server_default="0.30", nullable=False),
        sa.Column("new_order_leverage", sa.Float(), server_default="20.0", nullable=False),
        sa.Column("min_margin_ratio", sa.Float(), server_default="0.35", nullable=False),
        sa.Column("max_slippage_bps", sa.Float(), server_default="8.0", nullable=False),
        sa.Column("max_market_age_seconds", sa.Integer(), server_default="10", nullable=False),
        sa.Column("max_api_errors", sa.Integer(), server_default="3", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 品种映射表 ──────────────────────────────────────────────────────────
    op.create_table(
        "symbol_mappings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), unique=True, nullable=False),
        sa.Column("leg_a_venue_symbol", sa.String(64), nullable=False),
        sa.Column("mt5_symbol", sa.String(64), nullable=False),
        sa.Column("leg_a_venue", sa.String(32), server_default="hyperliquid", nullable=False),
        sa.Column("leg_a_symbol", sa.String(64), server_default="", nullable=False),
        sa.Column("leg_b_venue", sa.String(32), server_default="mt5", nullable=False),
        sa.Column("leg_b_symbol", sa.String(64), server_default="", nullable=False),
        sa.Column("base_asset", sa.String(32), server_default="", nullable=False),
        sa.Column("quote_asset", sa.String(32), server_default="USD", nullable=False),
        sa.Column("contract_multiplier", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("min_order_size", sa.Float(), server_default="0.001", nullable=False),
        sa.Column("min_entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("max_close_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mt5_min_lot", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mt5_volume_step", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mt5_contract_size", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("mt5_currency_base", sa.String(16), server_default="", nullable=False),
        sa.Column("mt5_currency_profit", sa.String(16), server_default="USD", nullable=False),
        sa.Column("mt5_currency_margin", sa.String(16), server_default="USD", nullable=False),
        sa.Column("mt5_calc_mode", sa.Integer(), server_default="0", nullable=False),
        sa.Column("mt5_min_base_size", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("leg_a_min_base_size", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("leg_a_min_notional", sa.Float(), server_default="10.0", nullable=False),
        sa.Column("execution_style", sa.String(64), server_default="taker_taker", nullable=False),
        sa.Column("hl_open_order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("hl_close_order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("hl_post_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("hl_maker_offset_bps", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("hl_order_ttl_seconds", sa.Integer(), server_default="3", nullable=False),
        sa.Column("hl_unfilled_action", sa.String(32), server_default="cancel", nullable=False),
        sa.Column("single_leg_action", sa.String(32), server_default="manual_intervention", nullable=False),
        sa.Column("mt5_open_order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("mt5_close_order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("mt5_session_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("mt5_session_auto_sync", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("mt5_session_template", sa.String(64), server_default="auto", nullable=False),
        sa.Column("mt5_session_timezone", sa.String(64), server_default="UTC", nullable=False),
        sa.Column("mt5_regular_sessions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("mt5_close_only_sessions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("mt5_quote_only_sessions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("mt5_session_source", sa.String(64), server_default="manual", nullable=False),
        sa.Column("mt5_session_last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("mt5_pre_close_no_open_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("mt5_post_open_cooldown_minutes", sa.Integer(), server_default="10", nullable=False),
        sa.Column("allow_hold_through_mt5_close", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("quantity_precision", sa.Integer(), server_default="4", nullable=False),
        sa.Column("price_precision", sa.Integer(), server_default="2", nullable=False),
        sa.Column("min_tick", sa.Float(), server_default="0.01", nullable=False),
        sa.Column("max_slippage_bps", sa.Float(), server_default="8.0", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_symbol_mappings_symbol", "symbol_mappings", ["symbol"], unique=True)

    # ── 账户快照表 ──────────────────────────────────────────────────────────
    op.create_table(
        "account_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("available_balance", sa.Float(), nullable=False),
        sa.Column("margin_used", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("margin_ratio", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("currency", sa.String(16), server_default="USD", nullable=False),
        sa.Column("portfolio_value", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("perp_equity", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("spot_balance", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("spot_hold", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("withdrawable", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("free_collateral", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("data_source", sa.String(64), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_account_snapshots_platform", "account_snapshots", ["platform"])

    # ── 持仓表 ──────────────────────────────────────────────────────────────
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=False),
        sa.Column("mark_price", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("margin_used", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("liquidation_price", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_positions_platform", "positions", ["platform"])
    op.create_index("ix_positions_symbol", "positions", ["symbol"])
    op.create_index("ix_positions_platform_symbol", "positions", ["platform", "symbol"])

    # ── 行情快照表 ──────────────────────────────────────────────────────────
    op.create_table(
        "market_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bid", sa.Float(), nullable=False),
        sa.Column("ask", sa.Float(), nullable=False),
        sa.Column("mid", sa.Float(), nullable=False),
        sa.Column("depth_notional", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_market_snapshots_platform", "market_snapshots", ["platform"])
    op.create_index("ix_market_snapshots_symbol", "market_snapshots", ["symbol"])
    op.create_index("ix_market_snapshots_symbol_platform", "market_snapshots", ["symbol", "platform"])

    # ── 当前价差表（按品种唯一）─────────────────────────────────────────────
    op.create_table(
        "spread_current",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), unique=True, nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("leg_a_bid", sa.Float(), nullable=False),
        sa.Column("leg_a_ask", sa.Float(), nullable=False),
        sa.Column("leg_b_bid", sa.Float(), nullable=False),
        sa.Column("leg_b_ask", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_b_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_a_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("notional_currency", sa.String(16), server_default="USD", nullable=False),
        sa.Column("fx_rate_to_usd", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("gross_spread", sa.Float(), nullable=False),
        sa.Column("entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("close_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mid_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("spread_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("net_profit", sa.Float(), nullable=False),
        sa.Column("annualized_return", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_spread_current_symbol", "spread_current", ["symbol"], unique=True)

    # ── 分方向当前价差表 ────────────────────────────────────────────────────
    op.create_table(
        "spread_direction_current",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("leg_a_bid", sa.Float(), nullable=False),
        sa.Column("leg_a_ask", sa.Float(), nullable=False),
        sa.Column("leg_b_bid", sa.Float(), nullable=False),
        sa.Column("leg_b_ask", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_b_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_a_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("notional_currency", sa.String(16), server_default="USD", nullable=False),
        sa.Column("fx_rate_to_usd", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("gross_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("close_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mid_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("spread_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("total_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("annualized_return", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("status", sa.String(32), server_default="candidate", nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_spread_direction_current_symbol", "spread_direction_current", ["symbol"])
    op.create_index("ix_spread_direction_current_direction", "spread_direction_current", ["direction"])

    # ── 价差时间桶表（OHLC 聚合）───────────────────────────────────────────
    op.create_table(
        "spread_buckets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("bucket_start", sa.DateTime(), nullable=False),
        sa.Column("bucket_seconds", sa.Integer(), server_default="5", nullable=False),
        sa.Column("open_spread", sa.Float(), nullable=False),
        sa.Column("high_spread", sa.Float(), nullable=False),
        sa.Column("low_spread", sa.Float(), nullable=False),
        sa.Column("close_spread", sa.Float(), nullable=False),
        sa.Column("avg_spread", sa.Float(), nullable=False),
        sa.Column("entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_close_basis_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_mid_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_spread_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_unit_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("avg_unit_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_spread_buckets_symbol", "spread_buckets", ["symbol"])
    op.create_index("ix_spread_buckets_direction", "spread_buckets", ["direction"])
    op.create_index("ix_spread_buckets_bucket_start", "spread_buckets", ["bucket_start"])
    op.create_index("ix_spread_buckets_symbol_dir_start", "spread_buckets", ["symbol", "direction", "bucket_start"])

    # ── 价差历史快照表 ──────────────────────────────────────────────────────
    op.create_table(
        "spread_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("leg_a_bid", sa.Float(), nullable=False),
        sa.Column("leg_a_ask", sa.Float(), nullable=False),
        sa.Column("leg_b_bid", sa.Float(), nullable=False),
        sa.Column("leg_b_ask", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_b_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_a_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("notional_currency", sa.String(16), server_default="USD", nullable=False),
        sa.Column("fx_rate_to_usd", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("gross_spread", sa.Float(), nullable=False),
        sa.Column("entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("close_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("mid_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("spread_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("net_profit", sa.Float(), nullable=False),
        sa.Column("annualized_return", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_spread_snapshots_symbol", "spread_snapshots", ["symbol"])
    op.create_index("ix_spread_snapshots_symbol_created", "spread_snapshots", ["symbol", "created_at"])

    # ── 套利机会记录表 ──────────────────────────────────────────────────────
    op.create_table(
        "arbitrage_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("leg_b_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_a_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("notional_currency", sa.String(16), server_default="USD", nullable=False),
        sa.Column("fx_rate_to_usd", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("gross_spread", sa.Float(), nullable=False),
        sa.Column("trigger_leg_a_bid", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_a_ask", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_b_bid", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_b_ask", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unit_net_profit", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("net_profit", sa.Float(), nullable=False),
        sa.Column("annualized_return", sa.Float(), nullable=False),
        sa.Column("entry_threshold", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("exit_target", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("overheat_threshold", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("signal_sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(32), server_default="candidate", nullable=False),
        sa.Column("reject_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_arbitrage_opportunities_symbol", "arbitrage_opportunities", ["symbol"])
    op.create_index("ix_arbitrage_opps_symbol_status", "arbitrage_opportunities", ["symbol", "status"])

    # ── 对冲组合表 ──────────────────────────────────────────────────────────
    op.create_table(
        "hedge_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), server_default="pending_open", nullable=False),
        sa.Column("execution_mode", sa.String(32), server_default="paper", nullable=False),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("leg_b_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("leg_a_quantity", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("open_cost", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("realized_pnl", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("fees", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("funding", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("swap", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_a_bid", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_a_ask", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_b_bid", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("trigger_leg_b_ask", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("entry_spread", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("entry_threshold", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("exit_target", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("overheat_threshold", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("close_reason", sa.Text(), server_default="", nullable=False),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(32), server_default="system", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_hedge_groups_symbol", "hedge_groups", ["symbol"])
    op.create_index("ix_hedge_groups_symbol_status", "hedge_groups", ["symbol", "status"])
    op.create_index("ix_hedge_groups_status_opened", "hedge_groups", ["status", "opened_at"])

    # ── 对冲组合事件表 ──────────────────────────────────────────────────────
    op.create_table(
        "hedge_group_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hedge_group_id", sa.Integer(), sa.ForeignKey("hedge_groups.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("detail", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_hedge_group_events_group_id", "hedge_group_events", ["hedge_group_id"])

    # ── 订单表 ──────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hedge_group_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("order_type", sa.String(16), server_default="market", nullable=False),
        sa.Column("post_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("status", sa.String(32), server_default="new", nullable=False),
        sa.Column("external_order_id", sa.String(128), server_default="", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_orders_hedge_group_id", "orders", ["hedge_group_id"])
    op.create_index("ix_orders_platform", "orders", ["platform"])
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_group_platform", "orders", ["hedge_group_id", "platform"])

    # ── 成交记录表 ──────────────────────────────────────────────────────────
    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("fee", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_fills_order_id", "fills", ["order_id"])
    op.create_index("ix_fills_platform", "fills", ["platform"])
    op.create_index("ix_fills_symbol", "fills", ["symbol"])
    op.create_index("ix_fills_order_platform", "fills", ["order_id", "platform"])

    # ── 盈亏快照表 ──────────────────────────────────────────────────────────
    op.create_table(
        "pnl_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hedge_group_id", sa.Integer(), nullable=True),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False),
        sa.Column("unrealized_pnl", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 系统日志表 ──────────────────────────────────────────────────────────
    op.create_table(
        "system_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(16), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_system_logs_level", "system_logs", ["level"])
    op.create_index("ix_system_logs_category", "system_logs", ["category"])
    op.create_index("ix_system_logs_created_at", "system_logs", ["created_at"])

    # ── 风控事件表 ──────────────────────────────────────────────────────────
    op.create_table(
        "risk_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(16), server_default="warning", nullable=False),
        sa.Column("rule", sa.String(128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("symbol", sa.String(32), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_risk_events_created_at", "risk_events", ["created_at"])

    # ── 告警表 ──────────────────────────────────────────────────────────────
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("level", sa.String(16), server_default="info", nullable=False),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # ── 工作线程运行记录表 ──────────────────────────────────────────────────
    op.create_table(
        "worker_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("worker_name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_worker_runs_worker_name", "worker_runs", ["worker_name"])


def downgrade() -> None:
    op.drop_table("worker_runs")
    op.drop_table("alerts")
    op.drop_table("risk_events")
    op.drop_table("system_logs")
    op.drop_table("pnl_snapshots")
    op.drop_table("fills")
    op.drop_table("orders")
    op.drop_table("hedge_group_events")
    op.drop_table("hedge_groups")
    op.drop_table("arbitrage_opportunities")
    op.drop_table("spread_snapshots")
    op.drop_table("spread_buckets")
    op.drop_table("spread_direction_current")
    op.drop_table("spread_current")
    op.drop_table("market_snapshots")
    op.drop_table("positions")
    op.drop_table("account_snapshots")
    op.drop_table("symbol_mappings")
    op.drop_table("risk_settings")
    op.drop_table("strategy_settings")
    op.drop_table("exchange_credentials")
    op.drop_table("system_settings")
    op.drop_table("audit_logs")
    op.drop_table("users")
