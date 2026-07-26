"""交易所凭证检查状态与执行门禁测试。"""

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, ExchangeCredential
from app.exchanges.credentials import encrypt_credentials, upsert_exchange_credential
from app.execution.readiness import _generic_live_venue_checks


def _db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)()


def test_connection_change_invalidates_previous_credential_check() -> None:
    db = _db()
    row = ExchangeCredential(
        venue="binance",
        display_name="Binance",
        environment="live",
        enabled=True,
        read_only=True,
        encrypted_credentials=encrypt_credentials({"api_key": "key", "api_secret": "secret"}),
        credentials_fingerprint="fingerprint",
        last_test_status="ok",
        last_test_message="passed",
        last_tested_at=datetime(2026, 7, 26, 12, 0, 0),
    )
    db.add(row)
    db.commit()

    updated = upsert_exchange_credential(db, {
        "venue": "binance",
        "display_name": "Binance",
        "environment": "live",
        "enabled": True,
        "read_only": False,
        "credentials": {},
    })

    assert updated.last_test_status == "untested"
    assert updated.last_test_message == ""
    assert updated.last_tested_at is None


def test_live_readiness_requires_latest_successful_credential_check() -> None:
    db = _db()
    row = ExchangeCredential(
        venue="binance",
        display_name="Binance",
        environment="live",
        enabled=True,
        read_only=False,
        encrypted_credentials=encrypt_credentials({"api_key": "key", "api_secret": "secret"}),
        credentials_fingerprint="fingerprint",
        last_test_status="failed",
        last_test_message="API Key 未启用 Futures 交易权限",
        last_tested_at=datetime(2026, 7, 26, 12, 0, 0),
    )
    db.add(row)
    db.commit()

    failed = _generic_live_venue_checks(db, {"binance"})[0]
    assert failed.status == "block"
    assert "完成凭证检查" in failed.message

    row.last_test_status = "ok"
    row.last_tested_at = datetime(2026, 7, 26, 12, 1, 0)
    db.commit()

    passed = _generic_live_venue_checks(db, {"binance"})[0]
    assert passed.status == "ok"
