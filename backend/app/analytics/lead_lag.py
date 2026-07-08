"""
领先-滞后分析模块
=================

检测两个交易平台（如 Hyperliquid 与 MT5）之间的价格领先-滞后关系。
通过分析报价变动的时间差和跟随率，帮助判断哪个平台具有价格发现优势。

主要功能：
- 检测领先平台的价格变动事件
- 衡量滞后平台的跟随率和延迟毫秒数
- 计算百分位延迟（P50 / P90）
- 生成可视化时间序列数据

使用方式::

    from app.analytics.lead_lag import lead_lag_report
    report = lead_lag_report("BTCUSD", window_seconds=300)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean

from app.core.time_utils import utc_now
from app.market.quotes import Quote, quote_cache


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeadLagEvent:
    """单个领先-滞后事件记录"""
    symbol: str                   # 品种名称
    leader_platform: str          # 领先平台
    follower_platform: str        # 滞后平台
    leader_time: datetime         # 领先平台变动时间
    follower_time: datetime | None  # 滞后平台跟随时间（未跟随则为 None）
    lag_ms: float | None          # 延迟毫秒数
    leader_move: float            # 领先平台价格变动
    follower_move: float          # 滞后平台价格变动
    leader_move_bps: float        # 领先平台变动基点数
    follower_move_bps: float      # 滞后平台变动基点数
    max_mid_diff: float           # 事件窗口内最大中间价差
    direction: str                # 变动方向（"up" / "down"）
    followed: bool                # 是否被跟随


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def lead_lag_report(
    symbol: str,
    window_seconds: int = 300,
    threshold_bps: float = 3.0,
    min_move: float = 0.0,
    follow_ratio: float = 0.5,
    max_lag_ms: int = 2000,
    leg_a_venue: str = "hyperliquid",
    leg_b_venue: str = "mt5",
) -> dict[str, object]:
    """生成领先-滞后分析报告。

    参数:
        symbol: 品种名称
        window_seconds: 分析时间窗口（秒）
        threshold_bps: 触发事件的最小变动阈值（基点）
        min_move: 绝对最小变动量
        follow_ratio: 跟随比例阈值（0-1）
        max_lag_ms: 最大允许延迟（毫秒）
        leg_a_venue: A 腿平台
        leg_b_venue: B 腿平台

    返回:
        包含事件列表、统计摘要和可视化序列的字典
    """
    symbol = symbol.upper()
    since = utc_now() - timedelta(seconds=max(window_seconds, 10))
    hl = [quote for quote in quote_cache.history(leg_a_venue, symbol) if quote.local_recv_ts >= since]
    mt5 = [quote for quote in quote_cache.history(leg_b_venue, symbol) if quote.local_recv_ts >= since]
    # 双向检测：A→B 和 B→A
    events = _events_for_direction(symbol, leg_a_venue, leg_b_venue, hl, mt5, threshold_bps, min_move, follow_ratio, max_lag_ms)
    events += _events_for_direction(symbol, leg_b_venue, leg_a_venue, mt5, hl, threshold_bps, min_move, follow_ratio, max_lag_ms)
    events.sort(key=lambda item: item.leader_time, reverse=True)
    summary = {
        "leg_a_to_leg_b": _summary(events, leg_a_venue, leg_b_venue),
        "leg_b_to_leg_a": _summary(events, leg_b_venue, leg_a_venue),
    }
    latest_hl = hl[-1] if hl else None
    latest_mt5 = mt5[-1] if mt5 else None
    return {
        "symbol": symbol,
        "window_seconds": max(window_seconds, 10),
        "threshold_bps": threshold_bps,
        "min_move": min_move,
        "follow_ratio": follow_ratio,
        "max_lag_ms": max_lag_ms,
        "latest": {
            leg_a_venue: _quote_dict(latest_hl),
            leg_b_venue: _quote_dict(latest_mt5),
        },
        "summary": summary,
        "items": [_event_dict(event) for event in events[:200]],
        "series": _series(hl, mt5, leg_a_venue, leg_b_venue),
    }


# ---------------------------------------------------------------------------
# 事件检测
# ---------------------------------------------------------------------------

def _events_for_direction(
    symbol: str,
    leader: str,
    follower: str,
    leader_quotes: list[Quote],
    follower_quotes: list[Quote],
    threshold_bps: float,
    min_move: float,
    follow_ratio: float,
    max_lag_ms: int,
) -> list[LeadLagEvent]:
    """检测单方向的领先-滞后事件。

    遍历领先平台的报价变动，筛选超过阈值的变动事件，
    然后在滞后平台报价中查找是否在 max_lag_ms 内出现同方向跟随。
    """
    if len(leader_quotes) < 2 or not follower_quotes:
        return []
    events: list[LeadLagEvent] = []
    cooldown_until = datetime.min
    for prev_quote, quote in zip(leader_quotes[:-1], leader_quotes[1:]):
        if quote.local_recv_ts < cooldown_until:
            continue
        move = quote.mid - prev_quote.mid
        move_bps = move / prev_quote.mid * 10_000 if prev_quote.mid else 0.0
        threshold = max(abs(prev_quote.mid) * threshold_bps / 10_000, min_move)
        if abs(move) < threshold:
            continue
        direction = "up" if move > 0 else "down"
        # 找到滞后平台在领先变动时刻的基准报价
        follower_base = _latest_at_or_before(follower_quotes, quote.local_recv_ts)
        if not follower_base:
            continue
        deadline = quote.local_recv_ts + timedelta(milliseconds=max_lag_ms)
        target_bps = abs(move_bps) * max(min(follow_ratio, 1.0), 0.0)
        # 在滞后平台报价中搜索跟随事件
        follower_hit = None
        for candidate in follower_quotes:
            if candidate.local_recv_ts <= quote.local_recv_ts:
                continue
            if candidate.local_recv_ts > deadline:
                break
            follower_move = candidate.mid - follower_base.mid
            follower_move_bps = follower_move / follower_base.mid * 10_000 if follower_base.mid else 0.0
            same_direction = (move > 0 and follower_move > 0) or (move < 0 and follower_move < 0)
            if same_direction and abs(follower_move_bps) >= target_bps:
                follower_hit = candidate
                break
        end_time = follower_hit.local_recv_ts if follower_hit else deadline
        follower_move = (follower_hit.mid - follower_base.mid) if follower_hit else 0.0
        follower_move_bps = follower_move / follower_base.mid * 10_000 if follower_base.mid else 0.0
        events.append(
            LeadLagEvent(
                symbol=symbol,
                leader_platform=leader,
                follower_platform=follower,
                leader_time=quote.local_recv_ts,
                follower_time=follower_hit.local_recv_ts if follower_hit else None,
                lag_ms=(follower_hit.local_recv_ts - quote.local_recv_ts).total_seconds() * 1000 if follower_hit else None,
                leader_move=move,
                follower_move=follower_move,
                leader_move_bps=move_bps,
                follower_move_bps=follower_move_bps,
                max_mid_diff=_max_mid_diff(symbol, quote.local_recv_ts, end_time, leader, follower),
                direction=direction,
                followed=bool(follower_hit),
            )
        )
        # 设置冷却期，避免重复检测
        cooldown_until = quote.local_recv_ts + timedelta(milliseconds=max_lag_ms)
    return events


def _latest_at_or_before(quotes: list[Quote], timestamp: datetime) -> Quote | None:
    """找到指定时间戳之前（含）的最新报价"""
    result = None
    for quote in quotes:
        if quote.local_recv_ts <= timestamp:
            result = quote
        else:
            break
    return result


def _max_mid_diff(symbol: str, start: datetime, end: datetime, leader: str = "hyperliquid", follower: str = "mt5") -> float:
    """计算事件窗口内两个平台之间的最大中间价差绝对值"""
    hl = [quote for quote in quote_cache.history(leader, symbol) if start <= quote.local_recv_ts <= end]
    mt5 = [quote for quote in quote_cache.history(follower, symbol) if start <= quote.local_recv_ts <= end]
    if not hl or not mt5:
        return 0.0
    values: list[float] = []
    for quote in hl:
        other = _latest_at_or_before(mt5, quote.local_recv_ts)
        if other:
            values.append(abs(quote.mid - other.mid))
    return max(values) if values else 0.0


# ---------------------------------------------------------------------------
# 统计与序列化
# ---------------------------------------------------------------------------

def _summary(events: list[LeadLagEvent], leader: str, follower: str) -> dict[str, object]:
    """计算单方向事件的统计摘要"""
    selected = [event for event in events if event.leader_platform == leader and event.follower_platform == follower]
    followed = [event for event in selected if event.followed and event.lag_ms is not None]
    lags = sorted(float(event.lag_ms) for event in followed if event.lag_ms is not None)
    return {
        "leader": leader,
        "follower": follower,
        "event_count": len(selected),
        "follow_count": len(followed),
        "follow_rate": len(followed) / len(selected) if selected else 0.0,
        "avg_lag_ms": mean(lags) if lags else None,
        "p50_lag_ms": _percentile(lags, 0.50),
        "p90_lag_ms": _percentile(lags, 0.90),
        "avg_max_mid_diff": mean([event.max_mid_diff for event in selected]) if selected else 0.0,
    }


def _series(hl: list[Quote], mt5: list[Quote], leg_a_venue: str = "hyperliquid", leg_b_venue: str = "mt5") -> list[dict[str, object]]:
    """生成两个平台中间价差的合并时间序列，用于前端图表渲染"""
    merged = sorted([(quote.local_recv_ts, quote.platform, quote.mid) for quote in hl + mt5], key=lambda item: item[0])
    latest: dict[str, float] = {}
    rows: list[dict[str, object]] = []
    for timestamp, platform, mid in merged:
        latest[platform] = mid
        rows.append(
            {
                "time": timestamp.isoformat(),
                "leg_a_mid": latest.get(leg_a_venue),
                "leg_b_mid": latest.get(leg_b_venue),
                "mid_diff": (latest[leg_a_venue] - latest[leg_b_venue]) if leg_a_venue in latest and leg_b_venue in latest else None,
            }
        )
    # 限制输出点数，超过 1000 时等间隔采样
    if len(rows) <= 1000:
        return rows
    step = max(len(rows) // 1000, 1)
    return rows[::step]


def _quote_dict(quote: Quote | None) -> dict[str, object] | None:
    """将 Quote 对象序列化为字典"""
    if not quote:
        return None
    return {
        "bid": quote.bid,
        "ask": quote.ask,
        "mid": quote.mid,
        "source": quote.source,
        "local_recv_ts": quote.local_recv_ts.isoformat(),
        "exchange_ts": quote.exchange_ts.isoformat() if quote.exchange_ts else None,
        "sequence": quote.sequence,
    }


def _event_dict(event: LeadLagEvent) -> dict[str, object]:
    """将 LeadLagEvent 序列化为字典"""
    return {
        "symbol": event.symbol,
        "leader_platform": event.leader_platform,
        "follower_platform": event.follower_platform,
        "leader_time": event.leader_time.isoformat(),
        "follower_time": event.follower_time.isoformat() if event.follower_time else None,
        "lag_ms": event.lag_ms,
        "leader_move": event.leader_move,
        "follower_move": event.follower_move,
        "leader_move_bps": event.leader_move_bps,
        "follower_move_bps": event.follower_move_bps,
        "max_mid_diff": event.max_mid_diff,
        "direction": event.direction,
        "followed": event.followed,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    """计算线性插值百分位数"""
    if not values:
        return None
    pct = min(max(percentile, 0.0), 1.0)
    index = pct * (len(values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    weight = index - lower
    return values[lower] * (1 - weight) + values[upper] * weight
