from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.settings import Settings, StorageSettings
from aitrace.ops.startup import StartupError, run_startup_checks, scan_for_credential_files


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


def _settings(tmp_path, **updates) -> Settings:
    base = Settings(
        service_token=SecretStr("test-token"),
        database={"dsn": "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"},
        runtime={"enabled": False},
        monitor={"enabled": False},
    )
    values = {"storage": StorageSettings(data_dir=tmp_path / "data")}
    values.update(updates)
    return base.model_copy(update=values)


def test_missing_data_dir_rejected(tmp_path, session_factory) -> None:
    settings = _settings(tmp_path, storage=StorageSettings(data_dir=tmp_path / "nope"))
    with pytest.raises(StartupError) as err:
        run_startup_checks(settings, session_factory)
    assert "数据根目录" in str(err.value)
    assert "建议" in str(err.value)


def test_credential_like_file_in_runtime_rejected(tmp_path, session_factory) -> None:
    data = tmp_path / "data"
    (data / "runtime").mkdir(parents=True)
    (data / "runtime" / "qoder-auth.json").write_text('{"token":"x"}', encoding="utf-8")
    with pytest.raises(StartupError) as err:
        run_startup_checks(_settings(tmp_path), session_factory)
    assert "qoder-auth.json" in str(err.value)
    assert "token" not in str(err.value)  # 内容不回显


def test_dev_mode_skips_release_checks(tmp_path, session_factory) -> None:
    data = tmp_path / "data"
    data.mkdir()
    run_startup_checks(_settings(tmp_path), session_factory)  # 不抛


def test_happy_path_creates_subdirs(tmp_path, session_factory) -> None:
    data = tmp_path / "data"
    data.mkdir()
    run_startup_checks(_settings(tmp_path), session_factory)
    for sub in ("evidence", "rawlogs", "runtime", "memory", "logs"):
        assert (data / sub).is_dir(), sub


def test_db_alembic_version_missing_rejected(tmp_path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    sqlite_factory = sessionmaker(bind=create_engine("sqlite://"), expire_on_commit=False)
    with pytest.raises(StartupError) as err:
        run_startup_checks(_settings(tmp_path), sqlite_factory)
    assert "alembic_version" in str(err.value)
    assert "建议" in str(err.value)


def test_release_root_qoder_binary_missing_rejected(tmp_path, session_factory) -> None:
    data = tmp_path / "data"
    data.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    (release / "REVISION").write_text("1.2.0", encoding="utf-8")
    with pytest.raises(StartupError) as err:
        run_startup_checks(_settings(tmp_path, release_root=release), session_factory)
    assert "qoder" in str(err.value)


def test_release_db_compat_behind_rejected(tmp_path, session_factory) -> None:
    data = tmp_path / "data"
    data.mkdir()
    release = tmp_path / "release"
    release.mkdir()
    (release / "REVISION").write_text("1.2.0", encoding="utf-8")
    qoder = release / "bin" / "qoder"
    qoder.parent.mkdir()
    qoder.write_text("#!/bin/sh\n", encoding="utf-8")
    qoder.chmod(0o755)
    (release / "DB_COMPAT").write_text("9999", encoding="utf-8")
    with pytest.raises(StartupError) as err:
        run_startup_checks(_settings(tmp_path, release_root=release), session_factory)
    assert "revision" in str(err.value)


def test_scan_for_credential_files_detects_name_and_content(tmp_path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "normal.log").write_text("plain text", encoding="utf-8")
    (runtime / "qoder-auth.json").write_text("{}", encoding="utf-8")
    (runtime / "notes.txt").write_text(
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----", encoding="utf-8"
    )
    runtime_hits, home_hits = scan_for_credential_files(runtime_dir=runtime, home_dir=None)
    assert len(runtime_hits) == 2
    assert len(home_hits) == 0
