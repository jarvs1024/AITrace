import os
import subprocess
import sys

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.settings import DatabaseSettings, RuntimeSettings, Settings

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set")

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, profile, memory_hot_file, case_contribution,
case_library, user_queue, task_queue, batch, report, "round", "session", callback_seq
RESTART IDENTITY CASCADE
"""


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(
            dsn=TEST_DATABASE_URL
            or "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"
        ),
        runtime=RuntimeSettings(enabled=False),
    )


@pytest.fixture(scope="session")
def db_engine() -> Engine:
    if TEST_DATABASE_URL is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DATABASE_URL}", "upgrade", "head"],
        check=True,
    )
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Session:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        session.execute(text(_TRUNCATE))
        session.commit()
        yield session
        session.rollback()
