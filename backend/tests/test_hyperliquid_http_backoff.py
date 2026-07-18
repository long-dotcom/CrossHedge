"""Hyperliquid HTTP 限流必须通过 Redis 在进程间共享退避。"""

from __future__ import annotations

from urllib.error import HTTPError

import pytest

from app.core import http_client
from app.core.redis_client import redis_client


def test_http_429_creates_shared_backoff_and_blocks_followup(monkeypatch) -> None:
    calls = 0

    def limited(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("https://api.hyperliquid.xyz/info", 429, "Too Many Requests", {"Retry-After": "3"}, None)

    monkeypatch.setattr(http_client.request, "urlopen", limited)

    with pytest.raises(http_client.HyperliquidRateLimitError, match="429"):
        http_client.post_hyperliquid_info("https://api.hyperliquid.xyz/info", {"type": "meta"})
    with pytest.raises(http_client.HyperliquidRateLimitError, match="退避中"):
        http_client.post_hyperliquid_info("https://api.hyperliquid.xyz/info", {"type": "allMids"})

    assert calls == 1
    assert redis_client().ttl(http_client._BACKOFF_KEY) > 0
