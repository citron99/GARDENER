# ruff: noqa: E402

import atexit
import os
import shutil
import tempfile
from pathlib import Path

test_root = Path(tempfile.mkdtemp(prefix="ai-garden-pytest-"))
test_db_path = test_root / "gardener.db"
test_upload_path = test_root / "uploads"
external_test_database_url = os.environ.get("TEST_DATABASE_URL")
os.environ["DATABASE_URL"] = external_test_database_url or f"sqlite:///{test_db_path.as_posix()}"
os.environ["UPLOAD_DIR"] = str(test_upload_path)
os.environ["JWT_SECRET"] = "test-only-secret-that-is-longer-than-32-characters"
os.environ["AI_SAFETY_SECRET"] = "different-test-safety-secret-longer-than-32-characters"
os.environ["PARTNER_ATTRIBUTION_SECRET"] = "third-test-partner-secret-longer-than-32-characters"
os.environ["AI_PROVIDER"] = "mock"
os.environ["ENVIRONMENT"] = "test"
os.environ["PARTNER_COMMERCE_ENABLED"] = "true"
os.environ["B2B_INVOICING_ENABLED"] = "true"
os.environ["B2B_INVOICE_ISSUER_NAME"] = "AI Garden Test SIA"
os.environ["B2B_INVOICE_ISSUER_REGISTRATION_NUMBER"] = "LV-TEST-40000000000"
os.environ["B2B_INVOICE_ISSUER_ADDRESS"] = "Testa iela 1, Riga, LV-1000"
os.environ["B2B_INVOICE_ISSUER_EMAIL"] = "billing@ai-garden.test"
os.environ["B2B_INVOICE_IBAN"] = "LV00TEST0000000000000"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app
from app.services.alert_service import reset_alert_rate_limits
from app.services.metrics_service import reset_metrics
from app.services.rate_limit_service import reset_local_rate_limits


def _remove_sqlite_files() -> None:
    if external_test_database_url:
        return
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(f"{test_db_path}{suffix}").unlink(missing_ok=True)


atexit.register(shutil.rmtree, test_root, True)


@pytest.fixture(autouse=True)
def clean_database():
    reset_local_rate_limits()
    reset_metrics()
    reset_alert_rate_limits()
    shutil.rmtree(test_upload_path, ignore_errors=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()
    _remove_sqlite_files()
    shutil.rmtree(test_upload_path, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_root():
    yield
    engine.dispose()
    _remove_sqlite_files()
    shutil.rmtree(test_root, ignore_errors=True)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
