"""首次启动时生成 CrossHedge 持久化密钥。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path


SECRETS_DIR = Path("/run/crosshedge-secrets")
SECRET_FILES = {"jwt_secret": 64, "exchange_config_secret": 64}


def create_secret(name: str, length: int, seed: str = "") -> None:
    """仅在密钥不存在时创建，容器重启不会覆盖已有值。"""
    path = SECRETS_DIR / name
    if path.exists():
        return
    value = seed.strip() or secrets.token_urlsafe(length)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(value)


def main() -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    legacy_jwt = os.getenv("LEGACY_JWT_SECRET", "").strip()
    legacy_exchange = os.getenv("LEGACY_EXCHANGE_CONFIG_SECRET", "").strip() or legacy_jwt
    create_secret("jwt_secret", SECRET_FILES["jwt_secret"], legacy_jwt)
    create_secret("exchange_config_secret", SECRET_FILES["exchange_config_secret"], legacy_exchange)


if __name__ == "__main__":
    main()
