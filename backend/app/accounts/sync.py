"""
账户同步模块
============

从各交易所（Hyperliquid / MT5 / Binance / Nautilus 支持的 venue）拉取账户快照，
写入 AccountSnapshot 表，供风控、前端展示和后续分析使用。

主要功能：
- 同步所有交易所的账户快照
- Hyperliquid 账户余额 / 保证金读取
- MT5 账户余额 / 保证金读取
- Binance / Nautilus 支持的交易所账户读取
- Paper 模拟账户初始化

使用方式::

    from app.accounts.sync import sync_account_snapshots, latest_account_snapshots
    snapshots = sync_account_snapshots(db)
"""

from __future__ import annotations

from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.adapters.nautilus import nautilus_account_snapshot
from app.adapters.paper import PaperAdapter
from app.adapters.venue import nautilus_venues_from_mappings
from app.config.settings import get_settings, hyperliquid_execution_info_url
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.core.mt5_bootstrap import ensure_mt5_connected
from app.db.models import AccountSnapshot, ExchangeCredential, SymbolMapping
from app.exchanges.credentials import mark_test_result


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def sync_account_snapshots(db: Session) -> list[AccountSnapshot]:
    """同步所有交易所的账户快照并写入数据库。

    流程：
    1. 读取 Hyperliquid 和 MT5 账户快照
    2. 遍历已启用的第三方交易所凭证，逐一拉取账户数据
    3. 将所有快照写入 AccountSnapshot 表

    参数:
        db: 数据库会话

    返回:
        最新的账户快照列表
    """
    snapshots = [_hyperliquid_account_snapshot(), _mt5_account_snapshot()]
    for credential in _enabled_exchange_credentials(db):
        try:
            snapshots.append(_configured_exchange_account_snapshot(credential))
        except Exception as exc:
            logger.warning("交易所账户读取失败: venue={}, error={}", credential.venue, exc)
            mark_test_result(credential, "failed", str(exc))
            snapshots.append(_configured_exchange_status_snapshot(credential, "error"))
    for snapshot in snapshots:
        db.add(snapshot)
    db.commit()
    return latest_account_snapshots(db)


def latest_account_snapshots(db: Session) -> list[AccountSnapshot]:
    """获取各平台最新的账户快照。

    遍历所有已知平台（hyperliquid / mt5 + Nautilus 支持的 venue），
    取每个平台 created_at 最新的一条记录。
    """
    rows: list[AccountSnapshot] = []
    platforms = ["hyperliquid", "mt5", *_enabled_nautilus_venues(db)]
    for platform in platforms:
        row = db.query(AccountSnapshot).filter(AccountSnapshot.platform == platform).order_by(desc(AccountSnapshot.created_at)).first()
        if row:
            rows.append(row)
    return rows


def ensure_initial_account_snapshots(db: Session) -> None:
    """确保数据库中存在初始账户快照（首次启动时使用 Paper 账户）。"""
    if db.query(AccountSnapshot).count():
        return
    for platform in ("hyperliquid", "mt5"):
        account = PaperAdapter(platform).get_account()
        db.add(
            AccountSnapshot(
                platform=account.platform,
                equity=account.equity,
                available_balance=account.available_balance,
                margin_used=account.margin_used,
                margin_ratio=account.margin_ratio,
                currency=account.currency,
            )
        )
    db.commit()


# ---------------------------------------------------------------------------
# 内部辅助：交易所 / venue 查询
# ---------------------------------------------------------------------------

def _enabled_nautilus_venues(db: Session) -> list[str]:
    """从品种映射和交易所凭证中收集所有已启用的 Nautilus venue"""
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = nautilus_venues_from_mappings(mappings)
    configured = db.query(ExchangeCredential.venue).filter(
        ExchangeCredential.enabled.is_(True),
        ExchangeCredential.venue.notin_(["hyperliquid", "mt5"]),
    ).all()
    for (venue,) in configured:
        if venue not in venues:
            venues.append(venue)
    return venues


def _enabled_exchange_credentials(db: Session) -> list[ExchangeCredential]:
    """获取所有已启用的第三方交易所凭证（排除 hyperliquid / mt5）"""
    return (
        db.query(ExchangeCredential)
        .filter(ExchangeCredential.enabled.is_(True), ExchangeCredential.venue.notin_(["hyperliquid", "mt5"]))
        .order_by(ExchangeCredential.venue)
        .all()
    )


def _configured_exchange_account_snapshot(row: ExchangeCredential) -> AccountSnapshot:
    """根据交易所凭证拉取账户快照。

    非原生交易所统一通过 Nautilus runtime 读取账户快照。
    """
    try:
        data = nautilus_account_snapshot(row.venue)
        return AccountSnapshot(**data)
    except Exception:
        return _configured_exchange_status_snapshot(row, row.last_test_status or "not_implemented")


def _configured_exchange_status_snapshot(row: ExchangeCredential, status: str) -> AccountSnapshot:
    """生成一个零值的交易所状态快照（用于错误 / 未实现状态）"""
    source = f"configured_{row.environment}_{status}"
    return AccountSnapshot(
        platform=row.venue,
        equity=0.0,
        available_balance=0.0,
        margin_used=0.0,
        margin_ratio=1.0,
        currency="USD",
        portfolio_value=0.0,
        perp_equity=0.0,
        withdrawable=0.0,
        free_collateral=0.0,
        data_source=source[:64],
    )


# ---------------------------------------------------------------------------
# Hyperliquid 账户快照
# ---------------------------------------------------------------------------

def _hyperliquid_account_snapshot() -> AccountSnapshot:
    """从 Hyperliquid 拉取账户状态并生成快照。

    使用统一的 post_hyperliquid_info 发送请求。
    拉取失败时回退到 Paper 模拟账户。
    """
    settings = get_settings()
    account_address = settings.hyperliquid.account_address
    if account_address:
        try:
            info_url = hyperliquid_execution_info_url(settings)
            data = post_hyperliquid_info(info_url, {"type": "clearinghouseState", "user": account_address})
            spot_data = post_hyperliquid_info(info_url, {"type": "spotClearinghouseState", "user": account_address})
            margin = data.get("marginSummary") or data.get("crossMarginSummary") or {}
            equity = float(margin.get("accountValue", 0.0))
            margin_used = float(margin.get("totalMarginUsed", 0.0))
            withdrawable = float(data.get("withdrawable", 0.0) or 0.0)
            spot_balance, spot_hold = _spot_usdc_balance(spot_data)
            spot_free = max(spot_balance - spot_hold, 0.0)
            free_collateral = max(withdrawable, spot_free)
            portfolio_value = spot_balance
            margin_ratio = (equity / margin_used) if margin_used > 0 else 1.0
            return AccountSnapshot(
                platform="hyperliquid",
                equity=portfolio_value,
                available_balance=free_collateral,
                margin_used=margin_used,
                margin_ratio=margin_ratio,
                currency="USDC",
                portfolio_value=portfolio_value,
                perp_equity=equity,
                spot_balance=spot_balance,
                spot_hold=spot_hold,
                withdrawable=withdrawable,
                free_collateral=free_collateral,
                data_source="hyperliquid_testnet" if "testnet" in info_url else "hyperliquid",
            )
        except Exception as exc:
            logger.warning("Hyperliquid 账户读取失败，回退 Paper 账户: {}", exc)
    # 回退到 Paper 模拟账户
    account = PaperAdapter("hyperliquid").get_account()
    return AccountSnapshot(
        platform=account.platform,
        equity=account.equity,
        available_balance=account.available_balance,
        margin_used=account.margin_used,
        margin_ratio=account.margin_ratio,
        currency=account.currency,
        portfolio_value=account.equity,
        perp_equity=account.equity,
        withdrawable=account.available_balance,
        free_collateral=account.available_balance,
        data_source="paper",
    )


# ---------------------------------------------------------------------------
# MT5 账户快照
# ---------------------------------------------------------------------------

def _mt5_account_snapshot() -> AccountSnapshot:
    """从 MT5 终端拉取账户状态并生成快照。

    使用 ensure_mt5_connected 统一连接逻辑。
    拉取失败时回退到 Paper 模拟账户。
    """
    settings = get_settings()

    # 使用统一的 MT5 连接辅助函数
    connected = ensure_mt5_connected(
        login=settings.mt5.login,
        password=settings.mt5.password,
        server=settings.mt5.server,
    )
    if not connected:
        logger.warning("MT5 连接失败，回退 Paper 账户")
        return _paper_mt5_account_snapshot()

    try:
        import MetaTrader5 as mt5  # type: ignore
        info = mt5.account_info()
        if not info:
            logger.warning("MT5 account_info 为空，回退 Paper 账户: {}", mt5.last_error())
            return _paper_mt5_account_snapshot()
        margin = float(getattr(info, "margin", 0.0) or 0.0)
        margin_level = float(getattr(info, "margin_level", 0.0) or 0.0)
        return AccountSnapshot(
            platform="mt5",
            equity=float(getattr(info, "equity", 0.0) or 0.0),
            available_balance=float(getattr(info, "margin_free", 0.0) or 0.0),
            margin_used=margin,
            margin_ratio=(margin_level / 100) if margin_level > 0 else 1.0,
            currency=str(getattr(info, "currency", "USD") or "USD"),
            portfolio_value=float(getattr(info, "equity", 0.0) or 0.0),
            perp_equity=float(getattr(info, "equity", 0.0) or 0.0),
            withdrawable=float(getattr(info, "margin_free", 0.0) or 0.0),
            free_collateral=float(getattr(info, "margin_free", 0.0) or 0.0),
            data_source="mt5_account_info",
        )
    except Exception as exc:
        logger.warning("MT5 账户读取失败，回退 Paper 账户: {}", exc)
        return _paper_mt5_account_snapshot()
    finally:
        try:
            import MetaTrader5 as mt5  # type: ignore
            mt5.shutdown()
        except Exception:
            pass


def _paper_mt5_account_snapshot() -> AccountSnapshot:
    """生成 MT5 Paper 模拟账户快照"""
    account = PaperAdapter("mt5").get_account()
    return AccountSnapshot(
        platform=account.platform,
        equity=account.equity,
        available_balance=account.available_balance,
        margin_used=account.margin_used,
        margin_ratio=account.margin_ratio,
        currency=account.currency,
        portfolio_value=account.equity,
        perp_equity=account.equity,
        withdrawable=account.available_balance,
        free_collateral=account.available_balance,
        data_source="paper",
    )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _spot_usdc_balance(data: dict) -> tuple[float, float]:
    """从 Hyperliquid 现货账户数据中提取 USDC 余额和冻结量"""
    for item in data.get("balances") or []:
        if item.get("coin") == "USDC":
            return float(item.get("total", 0.0) or 0.0), float(item.get("hold", 0.0) or 0.0)
    return 0.0, 0.0
