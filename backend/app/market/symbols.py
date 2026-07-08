"""
品种映射管理模块
=================

管理交易品种在两个交易所之间的映射关系：

- 从 YAML 配置文件加载品种映射到数据库
- 提供带 TTL 缓存的启用中品种映射查询
- 支持映射缓存清理

品种映射包含：
- A 腿（如 Hyperliquid）和 B 腿（如 MT5）的品种名、venue、精度等
- 下单参数（最小名义值、手数精度、订单类型等）
- 策略参数（价差阈值、滑点限制、单腿处理等）
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import yaml
from sqlalchemy.orm import Session

from app.config.settings import get_settings
from app.core.logging import get_logger
from app.db.models import SymbolMapping

logger = get_logger(__name__)

# 品种映射缓存：(bind_id, 缓存时间, 缓存数据)
_mapping_cache: tuple[int, float, list[SimpleNamespace]] = (0, 0.0, [])
_MAPPING_CACHE_TTL_SECONDS = 2.0


def load_symbol_mapping_file() -> list[dict]:
    """从 YAML 配置文件加载品种映射原始数据。

    返回:
        品种映射字典列表，每个字典包含一个品种的完整配置。
        文件不存在时返回空列表。
    """
    path = Path(get_settings().security.symbol_mapping_path)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("symbols", [])


def seed_symbol_mappings_from_file(db: Session) -> int:
    """将 YAML 配置文件中的品种映射导入数据库。

    仅导入数据库中尚不存在的品种（按 symbol 去重），
    已存在的品种不会被覆盖。

    参数:
        db: 数据库会话。

    返回:
        新导入的品种数量。
    """
    seeded = 0
    for item in load_symbol_mapping_file():
        symbol = item["symbol"]
        row = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol).first()
        if row:
            continue

        payload = {
            "leg_a_venue_symbol": item.get("leg_a_venue_symbol", item.get("hyperliquid_symbol", symbol)),
            "mt5_symbol": item.get("mt5_symbol", symbol),
            "leg_a_venue": item.get("leg_a_venue", "hyperliquid"),
            "leg_a_symbol": item.get("leg_a_symbol", item.get("leg_a_venue_symbol", item.get("hyperliquid_symbol", symbol))),
            "leg_b_venue": item.get("leg_b_venue", "mt5"),
            "leg_b_symbol": item.get("leg_b_symbol", item.get("mt5_symbol", symbol)),
            "base_asset": item.get("base_asset", symbol),
            "quote_asset": item.get("quote_asset", "USD"),
            "contract_multiplier": float(item.get("contract_multiplier", 1.0)),
            "min_order_size": float(item.get("min_order_size", 0.001)),
            "min_entry_spread": float(item.get("min_entry_spread", 0.0)),
            "max_close_spread": float(item.get("max_close_spread", 0.0)),
            "mt5_min_lot": float(item.get("mt5_min_lot", 0.0)),
            "mt5_volume_step": float(item.get("mt5_volume_step", 0.0)),
            "mt5_contract_size": float(item.get("mt5_contract_size", item.get("contract_multiplier", 1.0))),
            "mt5_currency_base": item.get("mt5_currency_base", ""),
            "mt5_currency_profit": item.get("mt5_currency_profit", item.get("quote_asset", "USD")),
            "mt5_currency_margin": item.get("mt5_currency_margin", item.get("quote_asset", "USD")),
            "mt5_calc_mode": int(item.get("mt5_calc_mode", 0)),
            "mt5_min_base_size": float(item.get("mt5_min_base_size", 0.0)),
            "leg_a_min_base_size": float(item.get("leg_a_min_base_size", item.get("hyperliquid_min_base_size", 0.0))),
            "leg_a_min_notional": float(item.get("leg_a_min_notional", item.get("hyperliquid_min_notional", 10.0))),
            "execution_style": item.get("execution_style", "taker_taker"),
            "hl_open_order_type": item.get("hl_open_order_type", "market"),
            "hl_close_order_type": item.get("hl_close_order_type", "market"),
            "hl_post_only": bool(item.get("hl_post_only", False)),
            "hl_maker_offset_bps": float(item.get("hl_maker_offset_bps", 1.0)),
            "hl_order_ttl_seconds": int(item.get("hl_order_ttl_seconds", 3)),
            "hl_unfilled_action": item.get("hl_unfilled_action", "cancel"),
            "single_leg_action": item.get("single_leg_action", "manual_intervention"),
            "mt5_open_order_type": item.get("mt5_open_order_type", "market"),
            "mt5_close_order_type": item.get("mt5_close_order_type", "market"),
            "mt5_pre_close_no_open_minutes": int(item.get("mt5_pre_close_no_open_minutes", 15)),
            "mt5_post_open_cooldown_minutes": int(item.get("mt5_post_open_cooldown_minutes", 10)),
            "allow_hold_through_mt5_close": bool(item.get("allow_hold_through_mt5_close", False)),
            "quantity_precision": int(item.get("quantity_precision", 4)),
            "price_precision": int(item.get("price_precision", 2)),
            "min_tick": float(item.get("min_tick", 0.01)),
            "max_slippage_bps": float(item.get("max_slippage_bps", 8.0)),
            "enabled": bool(item.get("enabled", True)),
        }
        db.add(SymbolMapping(symbol=symbol, **payload))
        seeded += 1

    db.commit()
    return seeded


def clear_symbol_mapping_cache() -> None:
    """清空品种映射缓存，强制下次查询重新从数据库加载。"""
    global _mapping_cache
    _mapping_cache = (0, 0.0, [])


def enabled_mappings(db: Session) -> list[SimpleNamespace]:
    """获取所有已启用的品种映射（带 TTL 缓存）。

    缓存策略：
    - 缓存有效期 2 秒
    - 按数据库连接 ID 区分，避免跨连接缓存污染
    - 返回 SimpleNamespace 列表，属性与 SymbolMapping 列一致

    参数:
        db: 数据库会话。

    返回:
        已启用品种映射的 SimpleNamespace 列表，按 symbol 排序。
    """
    global _mapping_cache
    now = monotonic()
    bind_id = id(db.get_bind())
    cached_bind_id, cached_at, cached = _mapping_cache
    if cached and cached_bind_id == bind_id and now - cached_at < _MAPPING_CACHE_TTL_SECONDS:
        return cached

    rows = (
        db.query(SymbolMapping)
        .filter(SymbolMapping.enabled.is_(True))
        .order_by(SymbolMapping.symbol)
        .all()
    )
    cached = [_snapshot_mapping(row) for row in rows]
    _mapping_cache = (bind_id, now, cached)
    return cached


def _snapshot_mapping(row: SymbolMapping) -> SimpleNamespace:
    """将数据库行转换为 SimpleNamespace 快照。

    自动填充默认值：
    - leg_a_venue 默认为 "hyperliquid"
    - leg_b_venue 默认为 "mt5"
    - 各腿 symbol 按优先级回退
    """
    values = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    values["leg_a_venue"] = values.get("leg_a_venue") or "hyperliquid"
    values["leg_a_symbol"] = (
        values.get("leg_a_symbol")
        or values.get("leg_a_venue_symbol")
        or values.get("hyperliquid_symbol")
        or values.get("symbol")
    )
    values["leg_b_venue"] = values.get("leg_b_venue") or "mt5"
    values["leg_b_symbol"] = (
        values.get("leg_b_symbol")
        or values.get("mt5_symbol")
        or values.get("symbol")
    )
    return SimpleNamespace(**values)
