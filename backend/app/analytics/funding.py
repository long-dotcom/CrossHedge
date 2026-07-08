"""
资金费分析模块
==============

提供永续合约资金费率（funding rate）的历史查询、统计汇总和分桶聚合功能。
支持 Hyperliquid 和 Binance 两个交易所的资金费数据源。

主要功能：
- 从 Hyperliquid / Binance 拉取资金费历史
- 按时间窗口（24h / 7d / 30d / 90d）统计资金费率
- 按粒度（raw / hour / day）分桶聚合

使用方式::

    from app.analytics.funding import funding_history
    result = funding_history(db, "BTCUSD", "7d", "day")
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.adapters.venue import mapping_leg
from app.config.settings import get_settings, hyperliquid_execution_info_url
from app.core.http_client import post_hyperliquid_info
from app.core.logging import get_logger
from app.db.models import ExchangeCredential, SymbolMapping
from app.exchanges.credentials import binance_futures_funding_history


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 时间窗口与分桶常量
# ---------------------------------------------------------------------------

# 查询时间窗口对应的秒数
RANGE_SECONDS = {
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
    "90d": 90 * 24 * 3600,
}

# 分桶粒度对应的秒数（0 表示不分桶，保留原始数据）
BUCKET_SECONDS = {
    "raw": 0,
    "hour": 3600,
    "day": 24 * 3600,
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class FundingPoint:
    """单个资金费数据点"""
    time: datetime                  # 资金费时间戳（naive UTC）
    funding_rate: float             # 资金费率
    premium: float | None = None    # 溢价（仅 Hyperliquid 提供）


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def funding_history(db: Session, symbol: str, range_value: str, bucket: str) -> dict:
    """查询指定品种的资金费历史，返回完整的资金费分析报告。

    参数:
        db: 数据库会话
        symbol: 交易品种名称
        range_value: 查询时间窗口（24h / 7d / 30d / 90d）
        bucket: 分桶粒度（raw / hour / day）

    返回:
        包含资金费历史、统计摘要、品种映射信息的字典
    """
    normalized_range = range_value if range_value in RANGE_SECONDS else "7d"
    normalized_bucket = bucket if bucket in BUCKET_SECONDS else "day"
    mapping = db.query(SymbolMapping).filter(SymbolMapping.symbol == symbol.upper()).first()
    funding_leg = _funding_leg(mapping, symbol)
    funding_venue, funding_symbol, funding_leg_name = funding_leg if funding_leg else ("", "", "")

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - RANGE_SECONDS[normalized_range] * 1000
    source_error = ""
    points: list[FundingPoint] = []
    credential: ExchangeCredential | None = None
    supported = funding_venue in {"hyperliquid", "binance"} and bool(funding_symbol)

    # 根据资金费来源交易所拉取数据
    if funding_venue == "hyperliquid" and funding_symbol:
        try:
            points = fetch_funding_history(funding_symbol, start_ms, end_ms)
        except Exception as exc:
            source_error = str(exc)
    elif funding_venue == "binance" and funding_symbol:
        credential = db.query(ExchangeCredential).filter(
            ExchangeCredential.venue == "binance",
            ExchangeCredential.enabled.is_(True),
        ).first()
        if credential:
            try:
                points = fetch_binance_funding_history(credential, funding_symbol, start_ms, end_ms)
            except Exception as exc:
                source_error = str(exc)
        else:
            supported = False
            source_error = "缺少已启用的 Binance 交易所配置，无法通过 Nautilus 读取资金费历史"
    else:
        source_error = "当前品种映射没有已支持 funding 的永续交易所腿，资金费历史暂不支持该 venue 组合"

    items = bucket_funding_points(points, normalized_bucket)
    leg_a_venue, leg_a_symbol = mapping_leg(mapping, "a") if mapping else ("hyperliquid", symbol.upper())
    leg_b_venue, leg_b_symbol = mapping_leg(mapping, "b") if mapping else ("mt5", "")
    return {
        "symbol": symbol.upper(),
        "leg_a_venue": leg_a_venue,
        "leg_a_symbol": leg_a_symbol,
        "leg_b_venue": leg_b_venue,
        "leg_b_symbol": leg_b_symbol,
        "funding_venue": funding_venue,
        "funding_symbol": funding_symbol,
        "funding_leg": funding_leg_name,
        "supported": supported,
        "leg_a_venue_symbol": funding_symbol,
        "range": normalized_range,
        "bucket": normalized_bucket,
        "summary": summarize_funding(points, normalized_range),
        "items": items,
        "source_error": source_error,
    }


def _funding_leg(mapping: SymbolMapping | None, symbol: str) -> tuple[str, str, str] | None:
    """确定品种映射中哪个交易所腿支持资金费查询。

    优先查找 hyperliquid / binance 这两个支持资金费的永续交易所。
    """
    if not mapping:
        return "hyperliquid", symbol.upper(), "a"
    funding_venues = {"hyperliquid", "binance"}
    for leg in ("a", "b"):
        venue, venue_symbol = mapping_leg(mapping, leg)
        if venue in funding_venues:
            return venue, venue_symbol, leg
    return None


# ---------------------------------------------------------------------------
# 数据拉取
# ---------------------------------------------------------------------------

def fetch_funding_history(coin: str, start_ms: int, end_ms: int) -> list[FundingPoint]:
    """从 Hyperliquid 拉取资金费历史。

    使用统一的 post_hyperliquid_info 发送请求。

    参数:
        coin: Hyperliquid 币种名称，如 "BTC"
        start_ms: 起始时间（毫秒时间戳）
        end_ms: 结束时间（毫秒时间戳）
    """
    settings = get_settings()
    payload = {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
    data = post_hyperliquid_info(hyperliquid_execution_info_url(settings), payload, timeout=15)
    points: list[FundingPoint] = []
    for item in data:
        timestamp_ms = int(item.get("time", 0))
        if not timestamp_ms:
            continue
        points.append(
            FundingPoint(
                time=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(tzinfo=None),
                funding_rate=float(item.get("fundingRate", 0.0)),
                premium=float(item["premium"]) if item.get("premium") is not None else None,
            )
        )
    return sorted(points, key=lambda point: point.time)


def fetch_binance_funding_history(credential: ExchangeCredential, symbol: str, start_ms: int, end_ms: int) -> list[FundingPoint]:
    """从 Binance（通过 Nautilus）拉取资金费历史。

    参数:
        credential: 交易所凭证行
        symbol: Binance 交易对符号
        start_ms: 起始时间（毫秒时间戳）
        end_ms: 结束时间（毫秒时间戳）
    """
    rows = binance_futures_funding_history(credential, symbol, start_ms, end_ms)
    points: list[FundingPoint] = []
    for item in rows:
        timestamp_ms = int(item.get("fundingTime", 0) or 0)
        if not timestamp_ms:
            continue
        points.append(
            FundingPoint(
                time=datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).replace(tzinfo=None),
                funding_rate=float(item.get("fundingRate", 0.0) or 0.0),
                premium=None,
            )
        )
    return sorted(points, key=lambda point: point.time)


# ---------------------------------------------------------------------------
# 统计汇总
# ---------------------------------------------------------------------------

def summarize_funding(points: list[FundingPoint], range_value: str) -> dict:
    """计算资金费数据点的统计摘要。

    返回包含样本数量、正负比例、平均/中位数/累计费率、年化估算等的字典。
    """
    if not points:
        return {
            "sample_count": 0,
            "positive_count": 0,
            "negative_count": 0,
            "positive_ratio": 0.0,
            "avg_funding_rate": 0.0,
            "median_funding_rate": 0.0,
            "sum_funding_rate": 0.0,
            "annualized_estimate": 0.0,
            "max_funding_rate": 0.0,
            "min_funding_rate": 0.0,
            "latest_funding_rate": 0.0,
            "bias": "no_data",
        }

    rates = [point.funding_rate for point in points]
    positive_count = sum(1 for value in rates if value > 0)
    negative_count = sum(1 for value in rates if value < 0)
    sum_rate = sum(rates)
    days = max(RANGE_SECONDS.get(range_value, RANGE_SECONDS["7d"]) / 86400, 1)
    annualized = (sum_rate / days) * 365
    positive_ratio = positive_count / len(rates)
    avg_rate = statistics.fmean(rates)

    # 判断资金费偏向：正费率占比 >= 65% 为正向偏向，<= 35% 为负向偏向
    if positive_ratio >= 0.65 and avg_rate > 0:
        bias = "positive"
    elif positive_ratio <= 0.35 and avg_rate < 0:
        bias = "negative"
    else:
        bias = "mixed"
    return {
        "sample_count": len(rates),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "positive_ratio": positive_ratio,
        "avg_funding_rate": avg_rate,
        "median_funding_rate": statistics.median(rates),
        "sum_funding_rate": sum_rate,
        "annualized_estimate": annualized,
        "max_funding_rate": max(rates),
        "min_funding_rate": min(rates),
        "latest_funding_rate": rates[-1],
        "bias": bias,
    }


# ---------------------------------------------------------------------------
# 分桶聚合
# ---------------------------------------------------------------------------

def bucket_funding_points(points: list[FundingPoint], bucket: str) -> list[dict]:
    """将资金费数据点按指定粒度分桶聚合。

    参数:
        points: 资金费数据点列表
        bucket: 分桶粒度（"raw" 保留原始数据，"hour" / "day" 按时段聚合）
    """
    # 原始模式：不分桶，直接输出
    if bucket == "raw":
        return [
            {
                "time": point.time.isoformat(),
                "avg_funding_rate": point.funding_rate,
                "sum_funding_rate": point.funding_rate,
                "positive_count": 1 if point.funding_rate > 0 else 0,
                "negative_count": 1 if point.funding_rate < 0 else 0,
                "count": 1,
                "premium": point.premium,
            }
            for point in points
        ]

    seconds = BUCKET_SECONDS[bucket]
    buckets: dict[datetime, list[FundingPoint]] = defaultdict(list)
    for point in points:
        if bucket == "day":
            # 按自然日分桶
            bucket_start = datetime(point.time.year, point.time.month, point.time.day)
        else:
            # 按固定秒数分桶
            epoch = int(point.time.replace(tzinfo=timezone.utc).timestamp())
            bucket_epoch = epoch - (epoch % seconds)
            bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc).replace(tzinfo=None)
        buckets[bucket_start].append(point)

    items = []
    for bucket_start in sorted(buckets):
        rows = buckets[bucket_start]
        rates = [row.funding_rate for row in rows]
        items.append(
            {
                "time": bucket_start.isoformat(),
                "avg_funding_rate": statistics.fmean(rates),
                "sum_funding_rate": sum(rates),
                "positive_count": sum(1 for value in rates if value > 0),
                "negative_count": sum(1 for value in rates if value < 0),
                "count": len(rates),
                "premium": None,
            }
        )
    return items
