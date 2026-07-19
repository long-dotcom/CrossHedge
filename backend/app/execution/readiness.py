"""
执行就绪检查模块
==================

为前端 API 提供执行层各组件的就绪状态检查：
- 实盘 / Paper 模式的 Hyperliquid 和 MT5 连接状态
- 品种映射完整性
- 外部仓位安全性（孤儿仓位、残余仓位检测）
- Paper-live 探针凭证检查

通过 Redis 心跳和快照检查 MT5 Gateway，
使用 ``post_hyperliquid_info`` 统一 Hyperliquid HTTP 调用。

使用方式::

    from app.core.db_session import db_session
    from app.execution.readiness import live_execution_readiness

    with db_session() as db:
        status = live_execution_readiness(db)
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger
from app.db.models import ExchangeCredential, HedgeGroup, Position, SymbolMapping, SystemSetting
from app.execution.runtime_settings import paper_live_probe_enabled_for_venue, runtime_paper_live_probe_enabled
from app.exchanges.credentials import build_credential_connector, decrypt_credentials
from app.venues.manager import native_venue_manager

logger = get_logger(__name__)

# 原生连接器当前支持的交易场所。
SUPPORTED_VENUES = {"hyperliquid", "mt5", "binance"}
PROBE_SUPPORTED_VENUES = SUPPORTED_VENUES - {"mt5"}


@dataclass(frozen=True)
class ReadinessCheck:
    """单项就绪检查结果。"""
    component: str
    status: str       # "ok" / "warn" / "block"
    message: str


def live_execution_readiness(db: Session, settings: Settings | None = None) -> dict:
    """实盘执行就绪检查。

    检查项：实盘总开关、Hyperliquid 账户、MT5 连接、品种映射、仓位安全。

    参数:
        db: 数据库会话。
        settings: 应用配置（可选，默认使用全局单例）。

    返回:
        ``{"status": str, "ready": bool, "checks": [...]}``
    """
    settings = settings or get_settings()
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = _mapped_venues(mappings)
    checks: list[ReadinessCheck] = []
    checks.extend(_global_live_checks(db))
    if "hyperliquid" in venues:
        checks.extend(_hyperliquid_live_checks(db, settings))
    if "mt5" in venues:
        checks.extend(_mt5_checks(settings))
    checks.extend(_generic_live_venue_checks(db, venues - {"hyperliquid", "mt5"}))
    checks.extend(_symbol_mapping_checks(db))
    checks.extend(_position_safety_checks(db))
    overall = _overall_status(checks)
    return {
        "status": overall,
        "ready": overall == "ready",
        "checks": [check.__dict__ for check in checks],
    }


def paper_execution_readiness(db: Session, settings: Settings | None = None) -> dict:
    """Paper 执行就绪检查。

    检查项：Hyperliquid paper 撮合、MT5 demo 连接、品种映射。

    参数:
        db: 数据库会话。
        settings: 应用配置（可选）。

    返回:
        ``{"status": str, "ready": bool, "checks": [...]}``
    """
    settings = settings or get_settings()
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    venues = _mapped_venues(mappings)
    probe_enabled = runtime_paper_live_probe_enabled(db, settings)
    checks: list[ReadinessCheck] = [ReadinessCheck(
        "paper_real_probe_mode",
        "ok" if probe_enabled else "block",
        "Paper 使用加密交易所真实最小单探针与 MT5 Demo" if probe_enabled else "Paper 仅支持真实最小单探针模式，请先开启总开关",
    )]
    if "hyperliquid" in venues:
        checks.extend(_hyperliquid_paper_checks(db, settings))
    else:
        checks.extend(_generic_paper_live_probe_checks(db, mappings, settings))
    if "mt5" in venues:
        checks.extend(_mt5_demo_checks(settings))
    checks.extend(_symbol_mapping_checks(db))
    overall = _overall_status(checks)
    return {
        "status": overall,
        "ready": overall == "ready",
        "checks": [check.__dict__ for check in checks],
    }


# ---------------------------------------------------------------------------
# 内部检查函数
# ---------------------------------------------------------------------------

def _mapped_venues(mappings: list[SymbolMapping]) -> set[str]:
    """提取当前实际启用的交易 venue。"""
    return {
        venue
        for mapping in mappings
        for venue in (str(mapping.leg_a_venue or "").strip().lower(), str(mapping.leg_b_venue or "").strip().lower())
        if venue
    }


def _generic_live_venue_checks(db: Session, venues: set[str]) -> list[ReadinessCheck]:
    """检查数据库管理的原生交易所实盘凭证。"""
    checks: list[ReadinessCheck] = []
    for venue in sorted(venues):
        credential = db.query(ExchangeCredential).filter(
            ExchangeCredential.venue == venue,
            ExchangeCredential.enabled.is_(True),
        ).first()
        ready = bool(credential and not credential.read_only and credential.encrypted_credentials)
        checks.append(ReadinessCheck(
            f"{venue}_live_credentials",
            "ok" if ready else "block",
            f"{venue} 实盘凭证已启用且允许交易" if ready else f"{venue} 需要启用交易所配置、填写凭证并关闭只读模式",
        ))
    return checks

def _global_live_checks(db: Session) -> list[ReadinessCheck]:
    """检查实盘交易总开关。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    enabled = bool(row and row.value == "true")
    return [
        ReadinessCheck(
            "global_live_switch",
            "ok" if enabled else "block",
            "系统实盘总开关已开启" if enabled else "系统实盘总开关未开启",
        )
    ]


def _hyperliquid_live_checks(db: Session, settings: Settings) -> list[ReadinessCheck]:
    """检查 Hyperliquid 实盘就绪状态。"""
    credential = _enabled_credential(db, "hyperliquid")
    values = decrypt_credentials(credential) if credential else {}
    user = str(values.get("account_address") or "")
    secret_key = str(values.get("secret_key") or "")
    checks = [
        ReadinessCheck(
            "hyperliquid_account_address",
            "ok" if user else "block",
            "Hyperliquid 账户地址已在管理台配置" if user else "请在管理台配置并启用 Hyperliquid 账户地址",
        ),
        ReadinessCheck(
            "hyperliquid_live_order_submit",
            "ok" if secret_key and credential and not credential.read_only else "block",
            "Hyperliquid 原生签名下单已配置" if secret_key and credential and not credential.read_only else "请在管理台配置签名密钥并关闭只读模式",
        ),
    ]
    if user:
        checks.append(_hyperliquid_read_probe(credential))
    return checks


def _hyperliquid_paper_checks(db: Session, settings: Settings) -> list[ReadinessCheck]:
    """检查 Hyperliquid 真实最小单探针就绪状态。"""
    mappings = db.query(SymbolMapping).filter(SymbolMapping.enabled.is_(True)).all()
    if not mappings:
        return [ReadinessCheck("hyperliquid_paper_matching", "block", "没有启用的品种映射，无法进行 Hyperliquid paper 撮合")]
    checks: list[ReadinessCheck] = []
    paper_live_hyperliquid = paper_live_probe_enabled_for_venue(db, settings, "hyperliquid")
    if paper_live_hyperliquid:
        checks.append(ReadinessCheck(
            "hyperliquid_paper_live_probe",
            "ok",
            f"Hyperliquid 使用真实最小单采样并立即回平，Paper 账本保留策略数量；已启用 {len(mappings)} 个品种",
        ))
        credential = _enabled_credential(db, "hyperliquid")
        values = decrypt_credentials(credential) if credential else {}
        user = str(values.get("account_address") or "")
        secret_key = str(values.get("secret_key") or "")
        checks.append(
            ReadinessCheck(
                "hyperliquid_paper_live_credentials",
                "ok" if user and secret_key and credential and not credential.read_only else "block",
                "Hyperliquid paper-live 凭证已在管理台配置" if user and secret_key and credential and not credential.read_only else "请在管理台配置 Hyperliquid 凭证并关闭只读模式",
            )
        )
        try:
            import hyperliquid.exchange  # noqa: F401
            import eth_account  # noqa: F401
            checks.append(ReadinessCheck("hyperliquid_sdk_import", "ok", "hyperliquid-python-sdk 可导入"))
        except Exception as exc:
            checks.append(ReadinessCheck("hyperliquid_sdk_import", "block", f"hyperliquid-python-sdk 不可导入: {exc}"))
    else:
        checks.append(ReadinessCheck("hyperliquid_paper_live_probe", "block", "Hyperliquid Paper 必须开启真实最小单探针"))
    checks.extend(_generic_paper_live_probe_checks(db, mappings, settings))
    return checks


def _generic_paper_live_probe_checks(db: Session, mappings: list[SymbolMapping], settings: Settings) -> list[ReadinessCheck]:
    """通用 paper-live 探针检查 —— 检查各 venue 的凭证和适配器支持。"""
    if not runtime_paper_live_probe_enabled(db, settings):
        return []
    mapped_venues = {
        venue
        for mapping in mappings
        for venue in (str(mapping.leg_a_venue or "").strip().lower(), str(mapping.leg_b_venue or "").strip().lower())
        if venue and venue != "mt5"
    }
    enabled_mapped = sorted(mapped_venues)
    checks = [
        ReadinessCheck(
            "paper_live_probe_venues",
            "ok" if enabled_mapped else "warn",
            f"通用 paper-live 探针 venue 已启用: {', '.join(enabled_mapped)}" if enabled_mapped else "PAPER_LIVE_PROBE_ENABLED 已开启，但当前启用品种映射未命中 PAPER_LIVE_PROBE_VENUES",
        )
    ]
    unsupported = sorted(mapped_venues - PROBE_SUPPORTED_VENUES - {"mt5"})
    if unsupported:
        checks.append(
            ReadinessCheck(
                "paper_live_probe_adapter_support",
                "warn",
                f"以下 venue 已进入通用探针框架，但真实探针下单 adapter 尚未实现: {', '.join(unsupported)}",
            )
        )
    for venue in sorted((mapped_venues & PROBE_SUPPORTED_VENUES) - {"hyperliquid"}):
        credential = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue, ExchangeCredential.enabled.is_(True)).first()
        ready = bool(credential and not credential.read_only and credential.encrypted_credentials)
        checks.append(
            ReadinessCheck(
                f"{venue}_paper_live_probe_credentials",
                "ok" if ready else "block",
                f"{venue} paper-live 探针凭证已启用且允许交易" if ready else f"{venue} paper-live 探针需要启用交易所配置、填写凭证并关闭只读模式",
            )
        )
    return checks


def _mt5_checks(settings: Settings) -> list[ReadinessCheck]:
    """检查 MT5 实盘就绪状态。"""
    return [_mt5_gateway_probe(expect_demo=False, require_trading=True)]


def _mt5_demo_checks(settings: Settings) -> list[ReadinessCheck]:
    """检查 MT5 demo 就绪状态。"""
    return [_mt5_gateway_probe(expect_demo=True, require_trading=True)]


def _symbol_mapping_checks(db: Session) -> list[ReadinessCheck]:
    """检查品种映射完整性。"""
    rows = db.query(SymbolMapping).filter(SymbolMapping.enabled == True).all()  # noqa: E712
    if not rows:
        return [ReadinessCheck("symbol_mappings", "block", "没有启用的品种映射")]
    checks = [ReadinessCheck("symbol_mappings", "ok", f"已启用 {len(rows)} 个品种映射")]
    missing_mt5_specs = [row.symbol for row in rows if not row.mt5_symbol or row.mt5_volume_step <= 0 or row.mt5_contract_size <= 0]
    if missing_mt5_specs:
        checks.append(ReadinessCheck("mt5_symbol_specs", "warn", f"以下品种 MT5 规格未完整同步: {', '.join(missing_mt5_specs)}"))
    else:
        checks.append(ReadinessCheck("mt5_symbol_specs", "ok", "启用品种 MT5 规格已同步"))
    auto_comp = [row.symbol for row in rows if row.single_leg_action in {"auto_close", "reverse_filled_leg"}]
    if auto_comp:
        checks.append(ReadinessCheck("single_leg_compensation", "warn", f"以下品种启用单腿自动反向冲销: {', '.join(auto_comp)}"))
    else:
        checks.append(ReadinessCheck("single_leg_compensation", "ok", "单腿异常默认人工介入"))
    return checks


def _position_safety_checks(db: Session) -> list[ReadinessCheck]:
    """检查外部仓位安全性 —— 检测残余仓位和孤儿仓位。"""
    positions = db.query(Position).filter(Position.platform.in_(list(SUPPORTED_VENUES))).all()
    active_positions = [row for row in positions if abs(row.quantity) > 0]
    if not active_positions:
        return [ReadinessCheck("live_position_management", "ok", "当前未发现已同步 live 仓位")]

    residual: list[str] = []
    orphan: list[str] = []
    for position in active_positions:
        matches = _live_groups_for_position(db, position)
        if not matches:
            orphan.append(_position_label(position))
            continue
        if any(group.status == "closed" for group in matches) and not any(group.status != "closed" for group in matches):
            residual.append(_position_label(position))

    checks: list[ReadinessCheck] = []
    if residual:
        checks.append(ReadinessCheck("live_residual_positions", "block", f"已关闭 live 对冲组仍存在残余仓位: {', '.join(residual)}"))
    if orphan:
        checks.append(ReadinessCheck("live_orphan_positions", "block", f"存在未归属 live 对冲组的外部仓位: {', '.join(orphan)}"))
    if not checks:
        checks.append(ReadinessCheck("live_position_management", "ok", "已同步 live 仓位均归属于系统对冲组"))
    return checks


# ---------------------------------------------------------------------------
# 仓位匹配辅助函数
# ---------------------------------------------------------------------------

def _live_groups_for_position(db: Session, position: Position) -> list[HedgeGroup]:
    """查找与给定仓位匹配的 live 对冲组。"""
    groups = db.query(HedgeGroup).filter(HedgeGroup.execution_mode == "live").all()
    return [group for group in groups if _position_matches_group(db, position, group)]


def _position_matches_group(db: Session, position: Position, group: HedgeGroup) -> bool:
    """判断仓位是否与对冲组匹配（平台、品种、方向、数量）。"""
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == group.symbol).first()
    leg_a_venue = mapping.leg_a_venue if mapping else "hyperliquid"
    leg_b_venue = mapping.leg_b_venue if mapping else "mt5"
    if position.platform not in {leg_a_venue, leg_b_venue}:
        return False
    symbols = {
        leg_a_venue: {group.symbol},
        leg_b_venue: {group.symbol},
    }
    if mapping:
        if mapping.leg_a_venue_symbol:
            symbols[leg_a_venue].add(mapping.leg_a_venue_symbol)
        if mapping.mt5_symbol:
            symbols[leg_b_venue].add(mapping.mt5_symbol)
    if position.symbol not in symbols.get(position.platform, set()):
        return False
    if _position_side(position.side) != _expected_position_side(group.direction, position.platform):
        return False
    if group.status == "closed":
        return True
    expected_quantity = _expected_position_quantity(group, position.platform)
    if expected_quantity <= 0:
        return False
    tolerance = max(expected_quantity * 0.000001, 0.00000001)
    return abs(abs(position.quantity) - expected_quantity) <= tolerance


def _expected_position_side(direction: str, platform: str) -> str:
    """根据对冲组方向推断指定平台上的预期仓位方向。"""
    if direction == "long_leg_a_short_leg_b":
        if platform == "hyperliquid":
            return "long"
        return "short"
    return "short" if platform == "hyperliquid" else "long"


def _expected_position_quantity(group: HedgeGroup, platform: str) -> float:
    """根据对冲组方向推断指定平台上的预期仓位数量。"""
    if platform == "hyperliquid":
        value = group.leg_a_quantity
    else:
        value = group.leg_b_quantity
    return float(group.quantity if value is None else value)


def _position_side(side: str) -> str:
    """标准化仓位方向字符串。"""
    value = str(side or "").strip().lower()
    if value in {"buy", "long"}:
        return "long"
    if value in {"sell", "short"}:
        return "short"
    return value


def _position_label(position: Position) -> str:
    """生成仓位标签（用于日志和告警展示）。"""
    return f"{position.platform}:{position.symbol}:{position.side}:{position.quantity}"


def _enabled_credential(db: Session, venue: str) -> ExchangeCredential | None:
    """获取管理台中已启用的交易所配置。"""
    return db.query(ExchangeCredential).filter(
        ExchangeCredential.venue == venue,
        ExchangeCredential.enabled.is_(True),
    ).first()


def _hyperliquid_read_probe(credential: ExchangeCredential) -> ReadinessCheck:
    """使用管理台保存的凭证执行 Hyperliquid 账户只读探测。"""
    try:
        build_credential_connector(credential).get_account()
        return ReadinessCheck("hyperliquid_read_probe", "ok", "Hyperliquid 账户只读探测成功")
    except Exception as exc:
        return ReadinessCheck("hyperliquid_read_probe", "block", f"Hyperliquid clearinghouseState 只读探测失败: {exc}")


def _mt5_gateway_probe(*, expect_demo: bool, require_trading: bool = False) -> ReadinessCheck:
    """通过 Redis 心跳和账户快照检查独立 MT5 Gateway。"""
    try:
        connector = native_venue_manager.connector_for("mt5", "live")
        health = connector.health()
        if not health.get("connected"):
            return ReadinessCheck("mt5_gateway", "block", str(health.get("error") or "MT5 Gateway 未连接"))
        if require_trading and health.get("read_only", True):
            return ReadinessCheck("mt5_gateway", "block", "MT5 Gateway 当前为只读模式，请在网关配置中开启对应下单开关")
        account = connector.get_account()
        if expect_demo:
            trade_mode = int(account.raw.get("trade_mode", -1))
            if trade_mode != 0:
                return ReadinessCheck("mt5_demo_account", "block", f"MT5 Gateway 当前不是 Demo 账户: {account.account_id}")
        name = "mt5_demo_account" if expect_demo else "mt5_read_probe"
        return ReadinessCheck(name, "ok", f"MT5 Gateway 账户快照可读: {account.account_id}".strip())
    except Exception as exc:
        return ReadinessCheck("mt5_gateway", "block", f"MT5 Gateway 探测失败: {exc}")


def _overall_status(checks: list[ReadinessCheck]) -> str:
    """汇总所有检查结果，返回整体状态。"""
    if any(check.status == "block" for check in checks):
        return "blocked"
    if any(check.status == "warn" for check in checks):
        return "warning"
    return "ready"
