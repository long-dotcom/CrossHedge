"""配置管理测试：Settings 加载、运行时安全策略、数据库 URL 解析、默认值验证。"""

from importlib import reload

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.security import hash_password
from app.db.models import Base, User
from app.db import init_db as init_db_module
from app.config.settings import SecuritySettings, Settings, enforce_runtime_security, get_settings, insecure_runtime_reasons


def test_runtime_security_allows_local_defaults() -> None:
    settings = Settings(environment="local")

    enforce_runtime_security(settings)

    assert insecure_runtime_reasons(settings)

def test_runtime_security_rejects_live_defaults_even_in_local() -> None:
    settings = Settings(environment="local", security=SecuritySettings(live_trading_enabled=True))

    with pytest.raises(RuntimeError, match="ADMIN_PASSWORD"):
        enforce_runtime_security(settings)

def test_runtime_security_rejects_production_defaults() -> None:
    settings = Settings(environment="production")

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        enforce_runtime_security(settings)

def test_runtime_security_accepts_production_custom_secrets() -> None:
    settings = Settings(
        environment="production",
        security=SecuritySettings(
            jwt_secret="a-prod-secret-with-enough-entropy",
            admin_password="not-the-default-password",
        ),
    )

    enforce_runtime_security(settings)

    assert insecure_runtime_reasons(settings) == []

def test_relative_sqlite_database_url_resolves_from_project_root(monkeypatch) -> None:
    import app.db.session as session_module

    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/mt5_hedge.db")
    try:
        session_module.get_settings.cache_clear()
        reloaded = reload(session_module)
        expected = (reloaded.ROOT_DIR / "data" / "mt5_hedge.db").as_posix()
        assert expected in str(reloaded.engine.url).replace("\\", "/")
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        session_module.get_settings.cache_clear()
        reload(session_module)

def test_seed_defaults_rejects_existing_default_admin_in_secure_runtime(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    db = Session()
    db.add(User(username="admin", password_hash=hash_password("admin123"), role="admin"))
    db.commit()
    monkeypatch.setattr(
        init_db_module,
        "get_settings",
        lambda: Settings(
            environment="production",
            security=SecuritySettings(jwt_secret="strong-secret", admin_password="changed-password"),
        ),
    )

    with pytest.raises(RuntimeError, match="默认密码"):
        init_db_module.seed_defaults(db)

    db.close()


def test_secret_files_override_environment_and_secure_redis_url(monkeypatch, tmp_path) -> None:
    jwt_file = tmp_path / "jwt_secret"
    exchange_file = tmp_path / "exchange_config_secret"
    redis_file = tmp_path / "redis_password"
    jwt_file.write_text("jwt-from-file", encoding="utf-8")
    exchange_file.write_text("exchange-from-file", encoding="utf-8")
    redis_file.write_text("redis-password", encoding="utf-8")
    monkeypatch.setenv("JWT_SECRET", "jwt-from-environment")
    monkeypatch.setenv("JWT_SECRET_FILE", str(jwt_file))
    monkeypatch.setenv("EXCHANGE_CONFIG_SECRET_FILE", str(exchange_file))
    monkeypatch.setenv("REDIS_URL", "redis://redis:16379/0")
    monkeypatch.setenv("REDIS_PASSWORD_FILE", str(redis_file))
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.security.jwt_secret == "jwt-from-file"
    assert settings.security.exchange_config_secret == "exchange-from-file"
    assert settings.redis.url == "redis://:redis-password@redis:16379/0"
    get_settings.cache_clear()
