"""
分Venue价差分析模块
===================

按交易所（venue）分别加载和分析买卖价差（bid-ask spread）时间序列。
主要服务于跨平台价差对比页面，提供：
- 按 venue 拆分的价差时间序列（优先从实时报价缓存获取，回退到数据库快照）
- 价差统计摘要（均值、标准差、中位数、P95、变异系数、异常占比）
- 降采样聚合（用于前端图表渲染）

数据来源：
- quote_cache：实时报价缓存，用于短周期（15m / 1h）的低延迟查询
- SpreadSnapshot 表：持久化价差快照，用于长周期或缓存不足时的回退

使用方式::

    from app.analytics.venue_spreads import venue_spread_report
    report = venue_spread_report(db, "BTCUSD", "1h")
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, median, pstdev
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.spreads import RANGE_SECONDS, TARGET_POINTS, parse_range
from app.db.models import SpreadSnapshot
from app.market.quotes import quote_cache


def load_venue_spread_series(
    db: Session,
    symbol: str,
    range_value: str,
    range_key: str | None = None,
    leg_a_venue: str = "hyperliquid",
    leg_b_venue: str = "mt5",
) -> list[dict[str, Any]]:
    """加载按 venue 拆分的买卖价差时间序列。

    参数:
        db: 数据库会话。
        symbol: 交易品种代码（如 ``"BTCUSD"``）。
        range_value: 时间窗口字符串，如 ``"1h"`` / ``"4h"`` / ``"24h"``。
        range_key: 已归一化的时间窗口标识符。若提供则用于报价缓存
            短周期判断，避免重复解析。
        leg_a_venue: A 腿交易所名称，默认 ``"hyperliquid"``。
        leg_b_venue: B 腿交易所名称，默认 ``"mt5"``。

    返回:
        时间序列列表，每项包含 ``time``、``leg_a_spread``、``leg_b_spread``。
    """
    key, start_at, seconds = parse_range(range_value)
    effective_key = range_key or key

    # 短周期优先从实时报价缓存获取，延迟更低
    if effective_key in ("15m", "1h"):
        series = _series_from_quote_cache(
            symbol, start_at, leg_a_venue=leg_a_venue, leg_b_venue=leg_b_venue
        )
        if len(series) >= 30:
            return series

    # 缓存数据不足或长周期：回退到数据库 SpreadSnapshot 表
    return _series_from_db(db, symbol, start_at)


def _series_from_quote_cache(
    symbol: str,
    start_at: datetime,
    leg_a_venue: str = "hyperliquid",
    leg_b_venue: str = "mt5",
) -> list[dict[str, Any]]:
    """从报价缓存构建按 venue 拆分的价差时间序列。

    通过将两腿报价按秒级时间桶对齐，逐条计算各 venue 的买卖价差。
    """
    hl_quotes = quote_cache.history(leg_a_venue, symbol)
    mt5_quotes = quote_cache.history(leg_b_venue, symbol)

    # 过滤起始时间之后的报价
    hl_quotes = [q for q in hl_quotes if q.local_recv_ts >= start_at]
    mt5_quotes = [q for q in mt5_quotes if q.local_recv_ts >= start_at]

    if not hl_quotes or not mt5_quotes:
        return []

    # 将 MT5 报价按秒级时间桶分组，便于快速查找
    mt5_by_ts: dict[int, list[Any]] = {}
    for q in mt5_quotes:
        bucket = int(q.local_recv_ts.timestamp())
        mt5_by_ts.setdefault(bucket, []).append(q)

    series: list[dict[str, Any]] = []
    for hl_q in hl_quotes:
        ts_bucket = int(hl_q.local_recv_ts.timestamp())
        mt5_candidates = mt5_by_ts.get(ts_bucket)
        if not mt5_candidates:
            # 尝试相邻秒（±1s）以容忍时钟偏差
            for delta in (-1, 1):
                mt5_candidates = mt5_by_ts.get(ts_bucket + delta)
                if mt5_candidates:
                    break
        if not mt5_candidates:
            continue
        mt5_q = mt5_candidates[-1]  # 取桶内最新报价
        series.append(
            {
                "time": hl_q.local_recv_ts.isoformat(),
                "leg_a_spread": hl_q.ask - hl_q.bid,
                "leg_b_spread": mt5_q.ask - mt5_q.bid,
            }
        )
    return series


def _series_from_db(
    db: Session, symbol: str, start_at: datetime
) -> list[dict[str, Any]]:
    """从数据库 SpreadSnapshot 表加载按 venue 拆分的价差序列。"""
    rows = (
        db.query(SpreadSnapshot)
        .filter(
            SpreadSnapshot.symbol == symbol.upper(),
            SpreadSnapshot.created_at >= start_at,
        )
        .order_by(SpreadSnapshot.created_at)
        .all()
    )
    return [
        {
            "time": row.created_at.isoformat(),
            "leg_a_spread": float(row.leg_a_ask) - float(row.leg_a_bid),
            "leg_b_spread": float(row.leg_b_ask) - float(row.leg_b_bid),
        }
        for row in rows
    ]


def summarize_venue_spreads(values: list[float]) -> dict[str, Any]:
    """计算单 venue 价差序列的统计摘要。

    参数:
        values: 价差值列表（按时间升序）。

    返回:
        包含 current / mean / std / min / max / median / p95 / cv /
        anomaly_pct / sample_count 的字典。
    """
    n = len(values)
    if n == 0:
        return {
            "current": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "median": 0.0,
            "p95": 0.0,
            "cv": 0.0,
            "anomaly_pct": 0.0,
            "sample_count": 0,
        }

    current = values[-1]
    avg = mean(values)
    std = pstdev(values) if n > 1 else 0.0
    sorted_vals = sorted(values)
    med = median(sorted_vals)

    # P95 计算
    p95_index = min(int(math.ceil(0.95 * n)) - 1, n - 1)
    p95 = sorted_vals[p95_index]

    # 变异系数（CV）= 标准差 / 均值
    cv = std / avg if avg != 0 else 0.0

    # 异常值占比：超过 3σ 的样本百分比
    anomaly_count = 0
    if std > 0:
        threshold = 3 * std
        anomaly_count = sum(1 for v in values if abs(v - avg) > threshold)
    anomaly_pct = anomaly_count / n * 100

    return {
        "current": current,
        "mean": round(avg, 6),
        "std": round(std, 6),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "median": round(med, 6),
        "p95": round(p95, 6),
        "cv": round(cv, 6),
        "anomaly_pct": round(anomaly_pct, 2),
        "sample_count": n,
    }


def downsample_venue_spreads(
    series: list[dict[str, Any]], range_value: str
) -> list[dict[str, Any]]:
    """对价差时间序列进行降采样，聚合为 OHLC + 均值格式。

    根据时间窗口的目标点数（TARGET_POINTS）计算桶宽度，
    将原始序列聚合为适合前端图表渲染的降采样结果。

    参数:
        series: 原始价差时间序列。
        range_value: 时间窗口字符串。

    返回:
        降采样后的列表，每项包含两腿的 open / close / high / low / avg 及 count。
    """
    if not series:
        return []

    key = range_value if range_value in TARGET_POINTS else "1h"
    target = TARGET_POINTS[key]
    seconds = RANGE_SECONDS[key]
    bucket_seconds = max(1, math.ceil(seconds / target))

    start_ts = datetime.fromisoformat(series[0]["time"]).timestamp()
    buckets: dict[int, list[dict[str, Any]]] = {}
    for point in series:
        ts = datetime.fromisoformat(point["time"]).timestamp()
        idx = int((ts - start_ts) // bucket_seconds)
        buckets.setdefault(idx, []).append(point)

    result: list[dict[str, Any]] = []
    for idx in sorted(buckets):
        bucket = buckets[idx]
        hl_vals = [p["leg_a_spread"] for p in bucket]
        mt5_vals = [p["leg_b_spread"] for p in bucket]
        result.append(
            {
                "time": bucket[-1]["time"],
                "leg_a_open": hl_vals[0],
                "leg_a_close": hl_vals[-1],
                "leg_a_high": max(hl_vals),
                "leg_a_low": min(hl_vals),
                "leg_a_avg": round(mean(hl_vals), 6),
                "leg_b_open": mt5_vals[0],
                "leg_b_close": mt5_vals[-1],
                "leg_b_high": max(mt5_vals),
                "leg_b_low": min(mt5_vals),
                "leg_b_avg": round(mean(mt5_vals), 6),
                "count": len(bucket),
            }
        )
    return result


def venue_spread_report(
    db: Session,
    symbol: str,
    range_value: str,
    range_key: str | None = None,
    leg_a_venue: str = "hyperliquid",
    leg_b_venue: str = "mt5",
) -> dict[str, Any]:
    """构建完整的按 venue 价差分析报告（主入口函数）。

    参数:
        db: 数据库会话。
        symbol: 交易品种代码。
        range_value: 时间窗口字符串。
        range_key: 已归一化的时间窗口标识符，透传给
            :func:`load_venue_spread_series`。
        leg_a_venue: A 腿交易所名称。
        leg_b_venue: B 腿交易所名称。

    返回:
        包含 symbol / range / summary（两腿统计摘要）/ series（降采样序列）
        的完整报告字典。
    """
    series = load_venue_spread_series(
        db,
        symbol,
        range_value,
        range_key=range_key,
        leg_a_venue=leg_a_venue,
        leg_b_venue=leg_b_venue,
    )

    hl_values = [p["leg_a_spread"] for p in series]
    mt5_values = [p["leg_b_spread"] for p in series]

    return {
        "symbol": symbol,
        "range": range_value,
        "summary": {
            "leg_a": summarize_venue_spreads(hl_values),
            "leg_b": summarize_venue_spreads(mt5_values),
        },
        "series": downsample_venue_spreads(series, range_value),
    }
