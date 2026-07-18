"""原生交易场所账户快照同步。"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.models import AccountSnapshot, ExchangeCredential
from app.exchanges.credentials import (
    SUPPORTED_VENUES,
    build_credential_connector,
    mark_test_result,
)
from app.venues.hyperliquid import HyperliquidConnector
from app.venues.mt5 import MT5Connector
from app.venues.paper import PaperConnector

logger = get_logger(__name__)


def sync_account_snapshots(db: Session) -> list[AccountSnapshot]:
    """同步所有启用的原生账户；单个场所失败不会阻断其他账户。"""
    rows = _enabled_exchange_credentials(db)
    configured = {row.venue for row in rows}
    snapshots: list[AccountSnapshot] = []

    for row in rows:
        try:
            connector = build_credential_connector(row)
            snapshots.append(_database_snapshot(connector.get_account(), source=f"{row.venue}_native"))
        except Exception as exc:
            logger.warning("原生账户读取失败: venue={}, error={}", row.venue, exc)
            mark_test_result(row, "failed", str(exc))
            snapshots.append(_status_snapshot(row.venue, row.environment, "error"))

    # 兼容旧版环境变量配置；迁移到 exchange_credentials 后不会重复拉取。
    if "hyperliquid" not in configured:
        snapshots.append(_legacy_hyperliquid_snapshot())
    if "mt5" not in configured:
        snapshots.append(_legacy_mt5_snapshot())

    for snapshot in snapshots:
        db.add(snapshot)
    db.commit()
    return latest_account_snapshots(db)


def latest_account_snapshots(db: Session) -> list[AccountSnapshot]:
    configured = [
        venue for (venue,) in db.query(ExchangeCredential.venue)
        .filter(ExchangeCredential.enabled.is_(True))
        .all()
        if venue in SUPPORTED_VENUES
    ]
    platforms = sorted(set(("hyperliquid", "mt5", *configured)))
    latest_ids = (
        db.query(func.max(AccountSnapshot.id).label("id"))
        .filter(AccountSnapshot.platform.in_(platforms))
        .group_by(AccountSnapshot.platform)
        .subquery()
    )
    return (
        db.query(AccountSnapshot)
        .join(latest_ids, AccountSnapshot.id == latest_ids.c.id)
        .order_by(AccountSnapshot.platform)
        .all()
    )


def ensure_initial_account_snapshots(db: Session) -> None:
    if db.query(AccountSnapshot).count():
        return
    for venue in ("hyperliquid", "mt5"):
        account = PaperConnector(venue=venue).get_account()
        db.add(_database_snapshot(account, source="paper"))
    db.commit()


def _enabled_exchange_credentials(db: Session) -> list[ExchangeCredential]:
    return (
        db.query(ExchangeCredential)
        .filter(
            ExchangeCredential.enabled.is_(True),
            ExchangeCredential.venue.in_(SUPPORTED_VENUES),
        )
        .order_by(ExchangeCredential.venue)
        .all()
    )


def _legacy_hyperliquid_snapshot() -> AccountSnapshot:
    settings = get_settings()
    if not settings.hyperliquid.account_address:
        return _database_snapshot(PaperConnector(venue="hyperliquid").get_account(), source="paper")
    try:
        connector = HyperliquidConnector(
            credentials={
                "account_address": settings.hyperliquid.account_address,
                "secret_key": settings.hyperliquid.secret_key,
            },
            read_only=True,
            info_url=settings.hyperliquid.info_url,
            ws_url=settings.hyperliquid.ws_url,
        )
        return _database_snapshot(connector.get_account(), source="hyperliquid_native")
    except Exception as exc:
        logger.warning("Hyperliquid 环境变量账户读取失败，使用状态快照: {}", exc)
        return _status_snapshot("hyperliquid", "live", "error")


def _legacy_mt5_snapshot() -> AccountSnapshot:
    settings = get_settings()
    connector = MT5Connector(
        credentials={
            "login": settings.mt5.login,
            "password": settings.mt5.password,
            "server": settings.mt5.server,
        },
        read_only=True,
    )
    try:
        return _database_snapshot(connector.get_account(), source="mt5_native")
    except Exception as exc:
        logger.warning("MT5 环境变量账户读取失败，使用状态快照: {}", exc)
        return _status_snapshot("mt5", "live", "error")


def _database_snapshot(account, *, source: str) -> AccountSnapshot:
    margin_used = float(account.margin_used)
    equity = float(account.equity)
    available = float(account.available_balance)
    spot_balance = 0.0
    spot_hold = 0.0
    for balance in account.balances:
        if balance.asset in {"USDC", "USDT", account.currency}:
            spot_balance += float(balance.wallet_balance)
            spot_hold += float(balance.locked_balance)
    return AccountSnapshot(
        platform=account.venue,
        equity=equity,
        available_balance=available,
        margin_used=margin_used,
        margin_ratio=(equity / margin_used) if margin_used > 0 else 1.0,
        currency=account.currency,
        portfolio_value=equity,
        perp_equity=equity,
        spot_balance=spot_balance,
        spot_hold=spot_hold,
        withdrawable=available,
        free_collateral=available,
        data_source=source[:64],
    )


def _status_snapshot(venue: str, environment: str, status: str) -> AccountSnapshot:
    return AccountSnapshot(
        platform=venue,
        equity=0.0,
        available_balance=0.0,
        margin_used=0.0,
        margin_ratio=1.0,
        currency="USD",
        portfolio_value=0.0,
        perp_equity=0.0,
        withdrawable=0.0,
        free_collateral=0.0,
        data_source=f"configured_{environment}_{status}"[:64],
    )
