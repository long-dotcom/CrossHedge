"""认证相关测试：Bearer token 解析、流式认证鉴权。"""

import pytest
from fastapi import HTTPException
from types import SimpleNamespace

from app.api import deps as api_deps


def test_stream_auth_rejects_missing_bearer_header() -> None:
    request = SimpleNamespace(headers={})

    with pytest.raises(HTTPException) as exc:
        api_deps.bearer_token_from_request(request)

    assert exc.value.status_code == 401

def test_stream_auth_reads_bearer_header_without_query_token() -> None:
    request = SimpleNamespace(headers={"authorization": "Bearer secure-token"})

    assert api_deps.bearer_token_from_request(request) == "secure-token"
