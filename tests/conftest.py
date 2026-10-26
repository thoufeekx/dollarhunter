import os

os.environ["DH_DATABASE_URL"] = os.environ.get("DH_DATABASE_URL", "").replace("/dollarhunter?", "/dollarhunter_test?") \
    if False else os.environ.get("DH_DATABASE_URL", "postgresql+psycopg://dollarhunter:dollarhunter@db:5432/dollarhunter_test")
# Force test values: docker compose loads .env, which must not leak into tests.
os.environ["DH_ADMIN_TOKEN"] = "test-admin-token"
os.environ["DH_STORAGE_BACKEND"] = "local"
os.environ["DH_AI_PROVIDER"] = "mock"
os.environ["DH_SMS_PROVIDER"] = "console"
os.environ["DH_STORAGE_DIR"] = "/tmp/dh_test_uploads"
os.environ["DH_PUBLIC_BASE_URL"] = "http://testserver"
os.environ["DH_DEFAULT_BUDGET_CAD"] = "25.00"  # the brief's seeded cap, used by its acceptance cases
for _k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET",
           "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DH_AI_API_KEY", "FIRECRAWL_API_KEY"):
    os.environ[_k] = ""  # real credentials must never be reachable from tests

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

url = make_url(get_settings().database_url)
assert url.database.endswith("_test"), "tests must run against a *_test database"
admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    if not c.execute(text("select 1 from pg_database where datname=:d"), {"d": url.database}).scalar():
        c.execute(text(f'create database "{url.database}"'))

from app.db import Base, engine, session_factory  # noqa: E402
import app.models  # noqa: E402,F401


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(engine())
    Base.metadata.create_all(engine())
    yield


@pytest.fixture
def sf():
    return session_factory()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
