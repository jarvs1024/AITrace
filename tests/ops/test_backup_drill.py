from __future__ import annotations

import hashlib
import os
import shutil

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

from aitrace.core.settings import BackupSettings, DatabaseSettings, Settings
from aitrace.ops.backup import run_backup, run_restore

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        os.getenv("AITRACE_OPS_DRILL") != "1",
        reason="恢复演练为破坏性操作，需 AITRACE_OPS_DRILL=1 显式开启（make ops）",
    ),
    pytest.mark.skipif(TEST_DATABASE_URL is None, reason="需要 TEST_DATABASE_URL"),
    pytest.mark.skipif(shutil.which("pg_dump") is None, reason="需要 pg_dump 在 PATH"),
    pytest.mark.skipif(shutil.which("pg_restore") is None, reason="需要 pg_restore 在 PATH"),
]


def _drill_settings(tmp_path) -> Settings:
    return Settings(
        service_token=SecretStr("drill-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL),
        backup=BackupSettings(
            staging_dir=tmp_path / "backups",
            keep_count=2,
            remote_dest=None,
            min_free_bytes=0,
        ),
    )


def test_backup_drill_real_dump_digest_and_list(tmp_path) -> None:
    settings = _drill_settings(tmp_path)
    result = run_backup(settings, staging=tmp_path / "backups")
    assert result.path.is_file()
    assert result.size_bytes > 0
    assert result.digest == hashlib.sha256(result.path.read_bytes()).hexdigest()
    digest_file = tmp_path / "backups" / (result.path.name + ".digest")
    assert digest_file.read_text(encoding="utf-8").startswith(result.digest)


def test_restore_drill_force_roundtrip(tmp_path) -> None:
    settings = _drill_settings(tmp_path)
    result = run_backup(settings, staging=tmp_path / "backups")
    engine = create_engine(TEST_DATABASE_URL)
    engine.dispose()
    run_restore(result.path, settings, dsn=TEST_DATABASE_URL, force=True)
    engine2 = create_engine(TEST_DATABASE_URL)
    try:
        with engine2.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one()
    finally:
        engine2.dispose()
    assert count == 1
