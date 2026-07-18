"""
风控引擎模块
============

提供交易前风控检查（pre-trade check）和风控事件记录功能。
根据风控模式、订单规模、滑点、保证金率等条件判断是否允许开仓。

主要功能：
- 获取当前风控配置（RiskSetting）
- 交易前多维度风控检查：模式、名义价值、滑点、保证金、行情时效
- 风控事件和告警记录

使用方式::

    from app.risk.engine import pre_trade_check, current_risk_setting
    decision = pre_trade_check(db, "BTCUSD", notional=50000, slippage_bps=5, market_time=utc_now())
    if not decision.allowed:
        print(f"风控拒绝: {decision.reason}")
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.accounts.sync import latest_account_snapshots
from app.core.logging import get_logger
from app.core.time_utils import utc_now
from app.db.models import Alert, HedgeGroup, RiskEvent, RiskSetting, SymbolMapping


logger = get_logger(__name__)

ACTIVE_OPEN_STATUSES = ("pending_open", "opening", "open", "open_partial", "closing", "manual_intervention")
PENDING_OPEN_STATUSES = ("pending_open", "opening")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class RiskDecision:
    """风控决策结果"""
    allowed: bool       # 是否允许交易
    reason: str = ""    # 拒绝原因（allowed=True 时为空）


# ---------------------------------------------------------------------------
# 风控配置
# ---------------------------------------------------------------------------

def current_risk_setting(db: Session) -> RiskSetting:
    """获取当前风控配置，不存在时自动创建默认配置。

    参数:
        db: 数据库会话

    返回:
        当前生效的 RiskSetting 记录
    """
    setting = db.query(RiskSetting).first()
    if not setting:
        setting = RiskSetting()
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting


# ---------------------------------------------------------------------------
# 交易前风控检查
# ---------------------------------------------------------------------------

def pre_trade_check(
    db: Session,
    symbol: str,
    notional: float,
    slippage_bps: float,
    market_time: datetime,
    use_live_account_risk: bool = True,
    direction: str = "",
) -> RiskDecision:
    """执行交易前多维度风控检查。

    检查维度（按顺序）：
    1. 风控模式：paused / emergency_stop / reduce_only 禁止开新仓
    2. 单笔名义价值上限
    3. 滑点上限
    4. 保证金充足率（使用实盘账户快照）
    5. 账户保证金率健康度
    6. 行情时效性

    参数:
        db: 数据库会话
        symbol: 品种名称
        notional: 订单名义价值（USD）
        slippage_bps: 预估滑点（基点）
        market_time: 行情时间戳
        use_live_account_risk: 是否使用实盘账户数据进行保证金检查

    返回:
        RiskDecision 对象，allowed=True 表示通过，否则包含拒绝原因
    """
    setting = current_risk_setting(db)

    # 1. 风控模式检查
    if setting.mode in {"paused", "emergency_stop", "reduce_only"}:
        return RiskDecision(False, f"当前风控模式为 {setting.mode}，禁止开新仓")

    # 2. 单笔名义价值检查
    if notional > setting.max_order_notional:
        return RiskDecision(False, f"单笔名义价值 {notional:.2f} USD 超过限制 {setting.max_order_notional:.2f} USD")

    capacity = open_capacity_check(db, symbol, direction, notional, setting=setting)
    if not capacity.allowed:
        return capacity

    # 3. 滑点检查
    if slippage_bps > setting.max_slippage_bps:
        return RiskDecision(False, "滑点超过限制")

    # 4-5. 保证金与账户健康度检查
    if use_live_account_risk:
        leverage = max(setting.new_order_leverage, 1.0)
        required_margin = notional / leverage
        accounts = latest_account_snapshots(db)
        if accounts:
            free_collateral = min((row.free_collateral or row.available_balance) for row in accounts)
            usable_margin = free_collateral * setting.max_new_margin_fraction
            if required_margin > usable_margin:
                return RiskDecision(False, f"新增保证金 {required_margin:.2f} 超过可用保证金折扣上限 {usable_margin:.2f}")
            weak_accounts = [row.platform for row in accounts if row.margin_ratio < setting.min_margin_ratio]
            if weak_accounts:
                return RiskDecision(False, f"账户保证金率低于阈值: {', '.join(weak_accounts)}")
            total_equity = sum(max(float(row.equity or 0.0), 0.0) for row in accounts)
            if setting.max_total_leverage > 0 and total_equity > 0:
                active_notional = _active_notional(db)
                # 每个对冲组包含两条近似等名义腿，总杠杆按双腿毛名义金额计算。
                projected_leverage = 2 * (active_notional + notional) / total_equity
                if projected_leverage > setting.max_total_leverage:
                    return RiskDecision(False, f"预计总杠杆 {projected_leverage:.2f} 超过限制 {setting.max_total_leverage:.2f}")

    # 6. 行情时效性检查
    age = (utc_now() - market_time).total_seconds()
    if age > setting.max_market_age_seconds:
        return RiskDecision(False, "行情已过期")

    return RiskDecision(True)


def open_capacity_check(
    db: Session,
    symbol: str,
    direction: str,
    notional: float,
    *,
    setting: RiskSetting | None = None,
) -> RiskDecision:
    """统一检查品种级、全局级以及每日开仓容量。"""
    setting = setting or current_risk_setting(db)
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
    if not mapping:
        return RiskDecision(False, "品种映射不存在")

    value = max(float(notional or 0.0), 0.0)
    active = db.query(HedgeGroup).filter(HedgeGroup.status.in_(ACTIVE_OPEN_STATUSES))
    symbol_active = active.filter(HedgeGroup.symbol == symbol)
    symbol_count = symbol_active.count()
    symbol_notional = float(symbol_active.with_entities(func.coalesce(func.sum(HedgeGroup.notional), 0.0)).scalar() or 0.0)
    global_count = active.count()
    global_notional = float(active.with_entities(func.coalesce(func.sum(HedgeGroup.notional), 0.0)).scalar() or 0.0)
    pending_count = db.query(HedgeGroup).filter(HedgeGroup.status.in_(PENDING_OPEN_STATUSES)).count()

    if symbol_count >= max(int(mapping.max_open_groups or 1), 1):
        return RiskDecision(False, f"{symbol} 未平对冲组已达上限 {mapping.max_open_groups}")
    if float(mapping.max_open_notional or 0.0) > 0 and symbol_notional + value > float(mapping.max_open_notional):
        return RiskDecision(False, f"{symbol} 累计名义金额 {symbol_notional + value:.2f} 超过品种上限 {mapping.max_open_notional:.2f}")
    if not bool(mapping.allow_opposite_direction) and direction:
        opposite_count = symbol_active.filter(HedgeGroup.direction != direction).count()
        if opposite_count:
            return RiskDecision(False, f"{symbol} 已有相反方向未平仓位，当前映射禁止双向同时持仓")

    if int(setting.max_global_open_groups or 0) > 0 and global_count >= int(setting.max_global_open_groups):
        return RiskDecision(False, f"全局未平对冲组已达上限 {setting.max_global_open_groups}")
    if float(setting.max_total_open_notional or 0.0) > 0 and global_notional + value > float(setting.max_total_open_notional):
        return RiskDecision(False, f"全局累计名义金额 {global_notional + value:.2f} 超过上限 {setting.max_total_open_notional:.2f}")
    if int(setting.max_pending_open_groups or 0) > 0 and pending_count >= int(setting.max_pending_open_groups):
        return RiskDecision(False, f"全局在途开仓组已达上限 {setting.max_pending_open_groups}")

    now = utc_now()
    latest_group_at = db.query(func.max(HedgeGroup.created_at)).filter(HedgeGroup.symbol == symbol).scalar()
    cooldown = max(int(mapping.open_cooldown_seconds or 0), 0)
    if latest_group_at and cooldown > 0:
        remaining = cooldown - (now - latest_group_at).total_seconds()
        if remaining > 0:
            return RiskDecision(False, f"{symbol} 开仓冷却中，还需 {remaining:.1f} 秒")

    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_groups = db.query(HedgeGroup).filter(HedgeGroup.symbol == symbol, HedgeGroup.created_at >= day_start)
    daily_count = daily_groups.count()
    daily_notional = float(daily_groups.with_entities(func.coalesce(func.sum(HedgeGroup.notional), 0.0)).scalar() or 0.0)
    if int(mapping.max_daily_opens or 0) > 0 and daily_count >= int(mapping.max_daily_opens):
        return RiskDecision(False, f"{symbol} 今日开仓次数已达上限 {mapping.max_daily_opens}")
    if float(mapping.max_daily_open_notional or 0.0) > 0 and daily_notional + value > float(mapping.max_daily_open_notional):
        return RiskDecision(False, f"{symbol} 今日累计开仓金额 {daily_notional + value:.2f} 超过上限 {mapping.max_daily_open_notional:.2f}")

    if float(setting.max_daily_loss or 0.0) > 0:
        daily_realized = float(db.query(func.coalesce(func.sum(HedgeGroup.realized_pnl), 0.0)).filter(HedgeGroup.closed_at >= day_start).scalar() or 0.0)
        if daily_realized <= -float(setting.max_daily_loss):
            return RiskDecision(False, f"今日已实现亏损 {abs(daily_realized):.2f} 已达到上限 {setting.max_daily_loss:.2f}")
    return RiskDecision(True)


def _active_notional(db: Session) -> float:
    return float(db.query(func.coalesce(func.sum(HedgeGroup.notional), 0.0)).filter(HedgeGroup.status.in_(ACTIVE_OPEN_STATUSES)).scalar() or 0.0)


# ---------------------------------------------------------------------------
# 风控事件记录
# ---------------------------------------------------------------------------

def record_risk_event(db: Session, rule: str, message: str, symbol: str = "", level: str = "warning") -> None:
    """记录风控触发事件和对应告警。

    参数:
        db: 数据库会话
        rule: 触发的风控规则名称
        message: 详细信息
        symbol: 相关品种
        level: 告警级别（默认 "warning"）
    """
    db.add(RiskEvent(rule=rule, message=message, symbol=symbol, level=level))
    db.add(Alert(level=level, title=f"风控触发：{rule}", message=message))
    db.commit()
