"""
认证安全模块
=============

提供密码哈希、JWT 令牌签发与验证功能：

- :func:`hash_password` —— 使用 PBKDF2-HMAC-SHA256 哈希密码
- :func:`verify_password` —— 验证密码与哈希是否匹配
- :func:`create_access_token` —— 签发 JWT 访问令牌
- :func:`decode_access_token` —— 解码并验证 JWT 访问令牌

安全说明：
    JWT 使用纯 HMAC-SHA256 手动实现（不依赖 PyJWT），
    密钥来自 ``settings.security.jwt_secret``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import timedelta
from typing import Any

from app.config.settings import get_settings
from app.core.time_utils import utc_now


# 延迟获取 settings，避免模块加载时的循环依赖
def _get_security_settings():
    """获取安全相关配置。"""
    return get_settings().security


def hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 哈希密码。

    格式为 ``{salt}${digest}``，salt 为 16 字节随机十六进制字符串。

    参数:
        password: 明文密码。

    返回:
        哈希后的密码字符串。
    """
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return f"{salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    """验证密码与哈希是否匹配。

    参数:
        password: 待验证的明文密码。
        password_hash: 已存储的哈希密码（格式 ``{salt}${digest}``）。

    返回:
        匹配返回 True，否则返回 False。
    """
    try:
        salt, digest = password_hash.split("$", 1)
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000
    ).hex()
    return hmac.compare_digest(candidate, digest)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    """签发 JWT 访问令牌。

    参数:
        subject: 令牌主体（通常为用户名）。
        extra: 额外的 payload 字段（可选）。

    返回:
        编码后的 JWT 字符串（``header.payload.signature``）。
    """
    sec = _get_security_settings()
    now = utc_now()
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=sec.access_token_minutes)).timestamp()),
    }
    if extra:
        payload.update(extra)

    header = {"alg": "HS256", "typ": "JWT"}
    encoded_header = _b64(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(f"{encoded_header}.{encoded_payload}", sec.jwt_secret)
    return f"{encoded_header}.{encoded_payload}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    """解码并验证 JWT 访问令牌。

    验证项：
    1. Token 格式（三段式）
    2. HMAC-SHA256 签名
    3. 过期时间

    参数:
        token: JWT 字符串。

    返回:
        解码后的 payload 字典。

    异常:
        ValueError: Token 格式无效、签名不匹配或已过期时抛出。
    """
    sec = _get_security_settings()
    try:
        encoded_header, encoded_payload, signature = token.split(".")
    except ValueError as exc:
        raise ValueError("无效 token") from exc

    expected = _sign(f"{encoded_header}.{encoded_payload}", sec.jwt_secret)
    if not hmac.compare_digest(signature, expected):
        raise ValueError("token 签名无效")

    payload = json.loads(_unb64(encoded_payload))
    if int(payload.get("exp", 0)) < int(utc_now().timestamp()):
        raise ValueError("token 已过期")
    return payload


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    """Base64 URL 安全编码（去除填充 ``=``）。"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(data: str) -> bytes:
    """Base64 URL 安全解码（自动补全填充）。"""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(data: str, secret: str) -> str:
    """使用 HMAC-SHA256 签名。"""
    digest = hmac.new(secret.encode("utf-8"), data.encode("ascii"), hashlib.sha256).digest()
    return _b64(digest)
