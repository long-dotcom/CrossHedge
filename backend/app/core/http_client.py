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
from urllib import request

from app.core.logging import get_logger

logger = get_logger(__name__)


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
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning(
            "Hyperliquid 请求失败: url={}, payload_type={}, error={}",
            url,
            payload.get("type", ""),
            exc,
        )
        raise
