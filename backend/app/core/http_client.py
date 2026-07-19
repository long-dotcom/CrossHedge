"""
Hyperliquid HTTP 客户端模块

统一 Hyperliquid JSON-RPC POST 请求逻辑，消除源项目中多处重复的
``_post_info`` / ``_post_hyperliquid_info`` 实现：
- ``adapters/hyperliquid.py`` 的 ``_post_info``
- ``analytics/funding.py`` 的 ``_post_hyperliquid_info``
- ``strategy/live_costs.py`` 的 ``_post_hyperliquid_info``
- ``execution/carry_costs.py`` 的 ``_post_hyperliquid_info``
- ``accounts/sync.py``、``market/active_refresh.py`` 等

使用方式::

    from app.core.http_client import post_hyperliquid_info

    data = post_hyperliquid_info("https://api.hyperliquid.xyz/info", {"type": "meta"})
"""

from __future__ import annotations

import json
import math
from urllib import request
from urllib.error import HTTPError

from app.core.logging import get_logger
from app.core.redis_client import redis_client, redis_key

logger = get_logger(__name__)


class HyperliquidRateLimitError(RuntimeError):
    """Hyperliquid 已限流或仍处于共享退避窗口。"""


_BACKOFF_KEY = redis_key("http", "hyperliquid", "backoff")
_STRIKES_KEY = redis_key("http", "hyperliquid", "rate-limit-strikes")


def post_hyperliquid_info(
    url: str,
    payload: dict,
    timeout: float = 10.0,
) -> dict | list:
    """向 Hyperliquid 发送 JSON-RPC POST 请求。

    统一所有 Hyperliquid ``/info`` 端点的 HTTP 调用，包含错误处理和日志记录。

    参数:
        url: Hyperliquid info 端点 URL，例如
            ``"https://api.hyperliquid.xyz/info"``。
        payload: JSON 请求体，例如 ``{"type": "meta"}``。
        timeout: HTTP 超时秒数，默认 10.0。

    返回:
        解析后的 JSON 响应（dict 或 list）。

    异常:
        Exception: 网络错误、超时或 JSON 解析失败时抛出，
            异常信息会通过 logger 记录。
    """
    remaining = redis_client().ttl(_BACKOFF_KEY)
    if remaining > 0:
        raise HyperliquidRateLimitError(f"Hyperliquid 限流退避中，约 {remaining} 秒后重试")

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            redis_client().delete(_STRIKES_KEY)
            return result
    except HTTPError as exc:
        if exc.code == 429:
            strikes = int(redis_client().incr(_STRIKES_KEY))
            redis_client().expire(_STRIKES_KEY, 300)
            header = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after = float(header) if header else 0.0
            except (TypeError, ValueError):
                retry_after = 0.0
            delay = max(retry_after, min(2 ** min(strikes, 6), 60))
            redis_client().set(_BACKOFF_KEY, str(delay), ex=max(math.ceil(delay), 1))
            logger.warning(
                "Hyperliquid 触发限流，进入共享退避: payload_type={}, delay_seconds={:.0f}",
                payload.get("type", ""), delay,
            )
            raise HyperliquidRateLimitError(f"Hyperliquid HTTP 429，退避 {delay:.0f} 秒") from exc
        logger.warning(
            "Hyperliquid 请求失败: url={}, payload_type={}, error={}",
            url, payload.get("type", ""), exc,
        )
        raise
    except Exception as exc:
        logger.warning(
            "Hyperliquid 请求失败: url={}, payload_type={}, error={}",
            url,
            payload.get("type", ""),
            exc,
        )
        raise
