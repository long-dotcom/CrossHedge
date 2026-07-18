"""
系统设置路由
============

涵盖策略、风控、品种映射、交易所凭据、执行参数、实盘交易等全部设置端点。

- GET  /settings/strategy                                  —— 策略设置
- PUT  /settings/strategy                                  —— 更新策略设置
- GET  /settings/risk                                      —— 风控设置
- PUT  /settings/risk                                      —— 更新风控设置
- GET  /settings/symbol-mappings                           —— 品种映射列表
- POST /settings/symbol-mappings                           —— 新建品种映射
- PUT  /settings/symbol-mappings                           —— 批量更新品种映射
- PUT  /settings/symbol-mappings/{mapping_id}              —— 更新单个品种映射
- DELETE /settings/symbol-mappings/{mapping_id}            —— 删除品种映射
- POST /settings/symbol-mappings/{mapping_id}/sync-broker  —— 从 MT5 同步品种信息
- POST /settings/symbol-mappings/{mapping_id}/sync-sessions —— 同步会话模板
- GET  /settings/exchanges                                 —— 交易所凭据列表
- POST /settings/exchanges                                 —— 新建交易所凭据
- PUT  /settings/exchanges/{venue}                         —— 更新交易所凭据
- DELETE /settings/exchanges/{venue}                       —— 删除交易所凭据
- POST /settings/exchanges/{venue}/test                    —— 测试交易所凭据
- GET  /settings/mt5-session-templates                     —— MT5 会话模板
- GET  /settings/live-trading                              —— 实盘交易状态
- PUT  /settings/live-trading                              —— 切换实盘交易
- GET  /settings/live-readiness                            —— 实盘就绪检查
- GET  /settings/paper-readiness                           —— Paper 就绪检查
- GET  /settings/execution                                 —— 执行参数
- PUT  /settings/execution                                 —— 更新执行参数
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import as_dict, _leg_metadata_for_symbol, _row_with_leg_metadata, audit
from app.venues.manager import native_venue_manager
from app.auth.dependencies import get_current_user, require_admin
from app.config.settings import get_settings
from app.db.models import (
    ArbitrageOpportunity,
    ExchangeCredential,
    RiskSetting,
    SpreadCurrent,
    SpreadDirectionCurrent,
    StrategySetting,
    SymbolMapping,
    SystemSetting,
    User,
)
from app.db.session import get_db
from app.exchanges.credentials import (
    mark_test_result,
    public_exchange_credential,
    upsert_exchange_credential,
    validate_exchange_credential,
)
from app.execution.readiness import live_execution_readiness, paper_execution_readiness
from app.execution.runtime_settings import execution_settings_payload, set_execution_settings
from app.market.mt5_schedule import apply_mt5_session_template, mt5_session_templates
from app.market.quotes import quote_cache
from app.market.symbols import clear_symbol_mapping_cache
from app.market.scan_state import scan_state_store
from app.schemas import (
    ExchangeCredentialIn,
    ExecutionSettingsIn,
    LiveTradingIn,
    RiskSettingsIn,
    StrategySettingsIn,
    SymbolMappingIn,
)
from app.strategy.statistical_signal import clear_signal_stats_cache
from app.execution.circuit_breaker import reload_config as reload_cb_config
from app.market.scanner import clear_strategy_setting_cache

router = APIRouter()


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _clear_scan_results_for_symbols(db: Session, symbols: set[str]) -> None:
    """清除指定品种的扫描结果（价差、方向、候选机会）。"""
    normalized = {s.upper() for s in symbols if s}
    if not normalized:
        return
    db.query(SpreadCurrent).filter(SpreadCurrent.symbol.in_(normalized)).delete(synchronize_session=False)
    db.query(SpreadDirectionCurrent).filter(SpreadDirectionCurrent.symbol.in_(normalized)).delete(synchronize_session=False)
    active_rows = db.query(ArbitrageOpportunity).filter(
        ArbitrageOpportunity.symbol.in_(normalized),
        ArbitrageOpportunity.status.in_(["candidate", "executable", "executing"]),
    ).all()
    for row in active_rows:
        row.status = "rejected"
        row.reject_reason = "品种映射已删除或停用，移出当前候选池"
    scan_state_store.remove_symbols(normalized)


def _effective_min_order_size(row: SymbolMapping) -> float:
    """根据 MT5 最小基数、leg_a 最小基数和报价计算有效最小下单量。"""
    from app.adapters.venue import mapping_leg
    leg_a_venue, _ = mapping_leg(row, "a")
    leg_a_quote = quote_cache.latest(leg_a_venue, row.symbol)
    leg_a_mid = leg_a_quote.mid if leg_a_quote else 0.0
    leg_a_notional_base = (row.leg_a_min_notional / leg_a_mid) if leg_a_mid > 0 and row.leg_a_min_notional > 0 else 0.0
    return max(row.mt5_min_base_size or 0.0, row.leg_a_min_base_size or 0.0, leg_a_notional_base)


def _decimal_places(value: float) -> int:
    """计算浮点数的小数位数。"""
    text = f"{value:.12f}".rstrip("0").rstrip(".")
    return len(text.split(".", 1)[1]) if "." in text else 0


# ---------------------------------------------------------------------------
# 策略设置
# ---------------------------------------------------------------------------

@router.get("/strategy")
def get_strategy(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取策略设置。"""
    return as_dict(db.query(StrategySetting).first())


@router.put("/strategy")
def put_strategy(
    payload: StrategySettingsIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新策略设置。"""
    row = db.query(StrategySetting).first() or StrategySetting()
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    audit(db, user.id, "update_strategy", "settings")
    db.commit()
    clear_strategy_setting_cache()
    clear_signal_stats_cache()
    reload_cb_config(db)
    return as_dict(row)


# ---------------------------------------------------------------------------
# 风控设置
# ---------------------------------------------------------------------------

@router.get("/risk")
def get_risk(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取风控设置。"""
    return as_dict(db.query(RiskSetting).first())


@router.put("/risk")
def put_risk(
    payload: RiskSettingsIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新风控设置。"""
    row = db.query(RiskSetting).first() or RiskSetting()
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    db.add(row)
    audit(db, user.id, "update_risk", "settings")
    db.commit()
    return as_dict(row)


# ---------------------------------------------------------------------------
# 品种映射
# ---------------------------------------------------------------------------

@router.get("/symbol-mappings")
def get_symbol_mappings(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """获取品种映射列表。"""
    return [_row_with_leg_metadata(db, r) for r in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()]


@router.put("/symbol-mappings")
def put_symbol_mappings(
    payload: list[SymbolMappingIn],
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """批量更新品种映射。"""
    stale_symbols: set[str] = set()
    for item in payload:
        row = db.query(SymbolMapping).filter(SymbolMapping.symbol == item.symbol).first()
        if not row:
            row = SymbolMapping(symbol=item.symbol, leg_a_venue_symbol=item.leg_a_venue_symbol, mt5_symbol=item.mt5_symbol)
        old_symbol = row.symbol
        for key, value in item.model_dump().items():
            setattr(row, key, value)
        if old_symbol != row.symbol or not row.enabled:
            stale_symbols.add(old_symbol)
        if not row.enabled:
            stale_symbols.add(row.symbol)
        db.add(row)
    _clear_scan_results_for_symbols(db, stale_symbols)
    audit(db, user.id, "update_symbol_mappings", "settings")
    db.commit()
    clear_symbol_mapping_cache()
    native_venue_manager.invalidate()
    clear_signal_stats_cache()
    return [_row_with_leg_metadata(db, r) for r in db.query(SymbolMapping).order_by(SymbolMapping.symbol).all()]


@router.post("/symbol-mappings")
def create_symbol_mapping(
    payload: SymbolMappingIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新建品种映射。"""
    if db.query(SymbolMapping).filter(SymbolMapping.symbol == payload.symbol).first():
        raise HTTPException(status_code=400, detail="内部品种已存在")
    row = SymbolMapping(**payload.model_dump())
    db.add(row)
    audit(db, user.id, "create_symbol_mapping", "settings", payload.symbol)
    db.commit()
    clear_symbol_mapping_cache()
    native_venue_manager.invalidate()
    clear_signal_stats_cache()
    db.refresh(row)
    return _row_with_leg_metadata(db, row)


@router.put("/symbol-mappings/{mapping_id}")
def update_symbol_mapping(
    mapping_id: int,
    payload: SymbolMappingIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新单个品种映射。"""
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    old_symbol = row.symbol
    duplicated = db.query(SymbolMapping).filter(
        SymbolMapping.symbol == payload.symbol, SymbolMapping.id != mapping_id
    ).first()
    if duplicated:
        raise HTTPException(status_code=400, detail="内部品种已存在")
    for key, value in payload.model_dump().items():
        setattr(row, key, value)
    stale_symbols = {old_symbol} if old_symbol != row.symbol or not row.enabled else set()
    if not row.enabled:
        stale_symbols.add(row.symbol)
    _clear_scan_results_for_symbols(db, stale_symbols)
    audit(db, user.id, "update_symbol_mapping", "settings", payload.symbol)
    db.commit()
    clear_symbol_mapping_cache()
    native_venue_manager.invalidate()
    clear_signal_stats_cache()
    db.refresh(row)
    return _row_with_leg_metadata(db, row)


@router.delete("/symbol-mappings/{mapping_id}")
def delete_symbol_mapping(
    mapping_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除品种映射。"""
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    symbol = row.symbol
    db.delete(row)
    _clear_scan_results_for_symbols(db, {symbol})
    audit(db, user.id, "delete_symbol_mapping", "settings", symbol)
    db.commit()
    clear_symbol_mapping_cache()
    native_venue_manager.invalidate()
    clear_signal_stats_cache()
    return {"status": "ok"}


@router.post("/symbol-mappings/{mapping_id}/sync-broker")
def sync_symbol_mapping_from_broker(
    mapping_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """从 MT5 终端同步品种信息到品种映射。"""
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"MetaTrader5 包不可用: {exc}") from exc
    if not mt5.initialize():
        raise HTTPException(status_code=400, detail=f"MT5 初始化失败: {mt5.last_error()}")
    mt5.symbol_select(row.mt5_symbol, True)
    info = mt5.symbol_info(row.mt5_symbol)
    if not info:
        raise HTTPException(status_code=400, detail="MT5 品种不存在或不可见")

    volume_min = float(getattr(info, "volume_min", row.min_order_size))
    volume_step = float(getattr(info, "volume_step", volume_min))
    contract_size = float(getattr(info, "trade_contract_size", row.contract_multiplier or 1.0))
    currency_base = str(getattr(info, "currency_base", "") or "")
    currency_profit = str(getattr(info, "currency_profit", row.quote_asset or "USD") or "USD")
    currency_margin = str(getattr(info, "currency_margin", currency_profit) or currency_profit)
    calc_mode = int(getattr(info, "trade_calc_mode", 0) or 0)
    digits = int(getattr(info, "digits", row.price_precision))
    tick_size = float(getattr(info, "trade_tick_size", 0.0) or getattr(info, "point", row.min_tick))
    mt5_min_base_size = volume_min * contract_size
    mt5_base_step = volume_step * contract_size

    row.mt5_min_lot = volume_min
    row.mt5_volume_step = volume_step
    row.mt5_contract_size = contract_size
    row.mt5_currency_base = currency_base
    row.mt5_currency_profit = currency_profit
    row.mt5_currency_margin = currency_margin
    row.mt5_calc_mode = calc_mode
    row.quote_asset = currency_profit or row.quote_asset
    row.mt5_min_base_size = mt5_min_base_size
    row.contract_multiplier = contract_size
    row.leg_a_min_notional = row.leg_a_min_notional or get_settings().hyperliquid.default_min_notional
    row.min_order_size = _effective_min_order_size(row)
    row.quantity_precision = max(_decimal_places(mt5_base_step), 0)
    row.price_precision = digits
    row.min_tick = tick_size

    audit(db, user.id, "sync_symbol_mapping_from_broker", "settings", row.symbol)
    db.commit()
    clear_symbol_mapping_cache()
    clear_signal_stats_cache()
    db.refresh(row)
    return {
        **as_dict(row),
        "broker": {
            "volume_min": volume_min,
            "volume_step": volume_step,
            "volume_max": float(getattr(info, "volume_max", 0.0)),
            "trade_contract_size": contract_size,
            "currency_base": currency_base,
            "currency_profit": currency_profit,
            "currency_margin": currency_margin,
            "trade_calc_mode": calc_mode,
            "digits": digits,
            "trade_tick_size": tick_size,
            "swap_long": float(getattr(info, "swap_long", 0.0)),
            "swap_short": float(getattr(info, "swap_short", 0.0)),
            "swap_mode": int(getattr(info, "swap_mode", 0)),
        },
    }


@router.post("/symbol-mappings/{mapping_id}/sync-sessions")
def sync_symbol_mapping_sessions(
    mapping_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """同步品种映射的 MT5 会话模板。"""
    row = db.get(SymbolMapping, mapping_id)
    if not row:
        raise HTTPException(status_code=404, detail="品种映射不存在")
    apply_mt5_session_template(row, row.mt5_session_template or "auto")
    db.add(row)
    audit(db, user.id, "sync_symbol_mapping_sessions", "settings", row.symbol)
    db.commit()
    clear_symbol_mapping_cache()
    db.refresh(row)
    return as_dict(row)


# ---------------------------------------------------------------------------
# 交易所凭据
# ---------------------------------------------------------------------------

@router.get("/exchanges")
def get_exchange_credentials(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """获取交易所凭据列表（脱敏）。"""
    rows = db.query(ExchangeCredential).order_by(ExchangeCredential.venue).all()
    return [public_exchange_credential(r) for r in rows]


@router.post("/exchanges")
def create_exchange_credential(
    payload: ExchangeCredentialIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """新建交易所凭据。"""
    try:
        row = upsert_exchange_credential(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, user.id, "upsert_exchange_credential", "settings", row.venue)
    db.commit()
    native_venue_manager.invalidate(row.venue)
    db.refresh(row)
    return public_exchange_credential(row)


@router.put("/exchanges/{venue}")
def update_exchange_credential(
    venue: str,
    payload: ExchangeCredentialIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新交易所凭据。"""
    if venue.strip().lower() != payload.venue.strip().lower():
        raise HTTPException(status_code=400, detail="路径 venue 与请求体 venue 不一致")
    try:
        row = upsert_exchange_credential(db, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit(db, user.id, "update_exchange_credential", "settings", row.venue)
    db.commit()
    native_venue_manager.invalidate(row.venue)
    db.refresh(row)
    return public_exchange_credential(row)


@router.delete("/exchanges/{venue}")
def delete_exchange_credential(
    venue: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除交易所凭据。"""
    row = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue.strip().lower()).first()
    if not row:
        raise HTTPException(status_code=404, detail="交易所配置不存在")
    db.delete(row)
    audit(db, user.id, "delete_exchange_credential", "settings", venue)
    db.commit()
    native_venue_manager.invalidate(venue)
    return {"status": "ok"}


@router.post("/exchanges/{venue}/test")
def test_exchange_credential(
    venue: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """测试交易所凭据连通性。"""
    row = db.query(ExchangeCredential).filter(ExchangeCredential.venue == venue.strip().lower()).first()
    if not row:
        raise HTTPException(status_code=404, detail="交易所配置不存在")
    try:
        status, message = validate_exchange_credential(row)
    except ValueError as exc:
        status, message = "failed", str(exc)
    mark_test_result(row, status, message)
    audit(db, user.id, "test_exchange_credential", "settings", row.venue)
    db.commit()
    db.refresh(row)
    return public_exchange_credential(row)


# ---------------------------------------------------------------------------
# MT5 会话模板
# ---------------------------------------------------------------------------

@router.get("/mt5-session-templates")
def get_mt5_session_templates(
    _: User = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """获取 MT5 会话模板列表。"""
    return mt5_session_templates()


# ---------------------------------------------------------------------------
# 实盘交易 / 执行参数 / 就绪检查
# ---------------------------------------------------------------------------

@router.get("/live-trading")
def get_live_trading(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取实盘交易状态。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first()
    return {"enabled": bool(row and row.value == "true"), "confirmation_required": "ENABLE LIVE TRADING"}


@router.put("/live-trading")
def put_live_trading(
    payload: LiveTradingIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """切换实盘交易开关。"""
    if payload.enabled and payload.confirmation != "ENABLE LIVE TRADING":
        raise HTTPException(status_code=400, detail="开启实盘需要输入确认短语")
    row = db.query(SystemSetting).filter(SystemSetting.key == "live_trading_enabled").first() or SystemSetting(key="live_trading_enabled")
    row.value = "true" if payload.enabled else "false"
    db.add(row)
    audit(db, user.id, "update_live_trading", "settings", row.value)
    db.commit()
    return {"enabled": row.value == "true"}


@router.get("/live-readiness")
def get_live_readiness(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """实盘就绪检查。"""
    return live_execution_readiness(db)


@router.get("/paper-readiness")
def get_paper_readiness(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Paper 模式就绪检查。"""
    return paper_execution_readiness(db)


@router.get("/execution")
def get_execution_settings(
    _: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取执行参数。"""
    return execution_settings_payload(db, get_settings())


@router.put("/execution")
def put_execution_settings(
    payload: ExecutionSettingsIn,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """更新执行参数。"""
    current = execution_settings_payload(db, get_settings())
    if payload.paper_live_probe_enabled and not current["paper_live_probe_enabled"] and payload.confirmation != "ENABLE PAPER LIVE PROBE":
        raise HTTPException(status_code=400, detail="开启 Paper 真实探针需要输入确认短语")
    set_execution_settings(
        db,
        paper_live_probe_enabled=payload.paper_live_probe_enabled,
        paper_live_parallel_execution=payload.paper_live_parallel_execution,
    )
    audit(db, user.id, "update_execution_settings", "settings", json.dumps(payload.model_dump(exclude={"confirmation"}), ensure_ascii=False))
    db.commit()
    return execution_settings_payload(db, get_settings())
