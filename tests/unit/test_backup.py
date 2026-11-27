from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine

from aitrace.core.settings import BackupSettings, DatabaseSettings, Settings, SettingsError
from aitrace.ops.backup import (
    BackupError,
    SpaceShortageError,
    _pg_env_parts,
    _retain_backups,
    console_main,
    run_backup,
    run_restore,
)


def _backup_settings(tmp_path: Path, **backup_updates) -> Settings:
    backup = {
        "staging_dir": tmp_path / "cfg-backups",
        "keep_count": 3,
        "remote_dest": None,
        "min_free_bytes": 0,
    }
    backup.update(backup_updates)
    return Settings(
        service_token=SecretStr("test-token"),
        database=DatabaseSettings(
            dsn="postgresql+psycopg://aitrace:s3cret@127.0.0.1:5432/aitrace_test"
        ),
        backup=BackupSettings(**backup),
    )


def test_dsn_split_keeps_password_out_of_argv() -> None:
    dsn = "postgresql+psycopg://aitrace:s3cret@127.0.0.1:5432/aitrace"
    conn, env = _pg_env_parts(dsn)
    assert "s3cret" not in conn
    assert env["PGPASSWORD"] == "s3cret"
    assert conn == "postgresql://aitrace@127.0.0.1:5432/aitrace"


def test_backup_skips_when_space_short(tmp_path) -> None:
    settings = _backup_settings(tmp_path, min_free_bytes=10**13)
    with pytest.raises(SpaceShortageError) as err:
        run_backup(settings, staging=tmp_path / "backups", keep=3)
    assert "可用空间不足" in str(err.value)
    assert not list((tmp_path / "backups").glob("*.dump"))


def test_retention_removes_oldest(tmp_path) -> None:
    staging = tmp_path / "backups"
    staging.mkdir()
    dumps = []
    for day in range(20, 25):
        file = staging / f"aitrace-202611{day}-010000.dump"
        file.write_bytes(b"x")
        dumps.append(file)
    stale_digest = staging / "aitrace-20261120-010000.dump.digest"
    stale_digest.write_text("deadbeef", encoding="utf-8")
    removed = _retain_backups(staging, keep=3)
    remaining = sorted(p.name for p in staging.glob("*.dump"))
    assert remaining == [f"aitrace-202611{day}-010000.dump" for day in (22, 23, 24)]
    assert len(removed) == 2
    assert {p.name for p in removed} == {
        "aitrace-20261120-010000.dump",
        "aitrace-20261121-010000.dump",
    }
    assert not stale_digest.exists()


def test_run_backup_produces_dump_digest_and_list_check(tmp_path) -> None:
    staging = tmp_path / "backups"
    settings = _backup_settings(tmp_path)
    calls: list[list[str]] = []
    seen_env: dict[str, str] = {}

    def fake_run(cmd, env=None, **kwargs):
        calls.append(list(cmd))
        if env is not None:
            seen_env.update(env)
        if cmd[0] == "pg_dump":
            target = Path(cmd[cmd.index("-f") + 1])
            target.write_bytes(b"dumpdata")
        return SimpleNamespace(returncode=0)

    with patch("aitrace.ops.backup.subprocess.run", side_effect=fake_run):
        result = run_backup(settings, staging=staging, keep=2)
    dumps = sorted(staging.glob("*.dump"))
    assert len(dumps) == 1
    assert dumps[0].read_bytes() == b"dumpdata"
    assert result.size_bytes == 8
    expected = hashlib.sha256(b"dumpdata").hexdigest()
    assert result.digest == expected
    digest_file = staging / (dumps[0].name + ".digest")
    assert digest_file.read_text(encoding="utf-8").startswith(expected)
    assert any(cmd[0] == "pg_restore" and "--list" in cmd for cmd in calls)
    assert all("s3cret" not in " ".join(cmd) for cmd in calls)
    assert seen_env["PGPASSWORD"] == "s3cret"


def test_push_failure_keeps_dump_and_logs_warning(tmp_path, caplog) -> None:
    staging = tmp_path / "backups"
    remote = tmp_path / "remote"
    settings = _backup_settings(tmp_path, remote_dest=str(remote))

    def fake_run(cmd, env=None, **kwargs):
        if cmd[0] == "pg_dump":
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"dumpdata")
        return SimpleNamespace(returncode=0)

    def fake_copy2(src, dst):
        raise OSError("remote disk gone")

    with (
        patch("aitrace.ops.backup.subprocess.run", side_effect=fake_run),
        patch("aitrace.ops.backup.shutil.copy2", side_effect=fake_copy2),
    ):
        result = run_backup(settings, staging=staging)
    assert result.pushed is False
    assert (staging / result.path.name).is_file()
    assert any("异盘推送失败" in record.message for record in caplog.records)


def test_restore_refuses_nonempty_target_without_force(tmp_path) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")
    settings = _backup_settings(tmp_path)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE task_queue (id INTEGER)")
        conn.exec_driver_sql("INSERT INTO task_queue VALUES (1)")

    with patch("aitrace.ops.backup.subprocess.run") as fake_run, pytest.raises(BackupError) as err:
        run_restore(dump, settings, engine_factory=lambda: engine)
    assert "非空" in str(err.value)
    fake_run.assert_not_called()


def test_restore_force_runs_pg_restore_without_password_in_argv(tmp_path) -> None:
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")
    settings = _backup_settings(tmp_path)
    with patch(
        "aitrace.ops.backup.subprocess.run", return_value=SimpleNamespace(returncode=0)
    ) as fake_run:
        run_restore(
            dump,
            settings,
            dsn="postgresql+psycopg://aitrace:s3cret@127.0.0.1:5432/aitrace_test",
            force=True,
        )
    cmd = fake_run.call_args.args[0]
    assert cmd[0] == "pg_restore"
    assert "--clean" in cmd and "--if-exists" in cmd
    assert str(dump) in cmd
    assert "s3cret" not in " ".join(cmd)
    assert fake_run.call_args.kwargs["env"]["PGPASSWORD"] == "s3cret"


def test_console_backup_space_short_returns_3(tmp_path, capsys) -> None:
    settings = _backup_settings(tmp_path, min_free_bytes=10**13)
    with patch("aitrace.ops.backup.load_settings", return_value=settings):
        code = console_main(["backup"])
    assert code == 3
    assert "可用空间不足" in capsys.readouterr().err


def test_console_backup_success_returns_0(tmp_path, capsys) -> None:
    settings = _backup_settings(tmp_path)

    def fake_run(cmd, env=None, **kwargs):
        if cmd[0] == "pg_dump":
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"dumpdata")
        return SimpleNamespace(returncode=0)

    with (
        patch("aitrace.ops.backup.load_settings", return_value=settings),
        patch("aitrace.ops.backup.subprocess.run", side_effect=fake_run),
    ):
        code = console_main(["backup", "--staging", str(tmp_path / "cli-backups")])
    assert code == 0
    out = capsys.readouterr().out
    assert "备份完成" in out
    assert list((tmp_path / "cli-backups").glob("*.dump"))


def test_console_backup_pg_dump_failure_returns_1(tmp_path, capsys) -> None:
    settings = _backup_settings(tmp_path)
    with (
        patch("aitrace.ops.backup.load_settings", return_value=settings),
        patch(
            "aitrace.ops.backup.subprocess.run",
            return_value=SimpleNamespace(returncode=1, stderr="pg_dump: connection refused"),
        ),
    ):
        code = console_main(["backup", "--staging", str(tmp_path / "cli-backups")])
    assert code == 1
    assert "数据库备份失败" in capsys.readouterr().err


def test_console_restore_error_returns_1(tmp_path, capsys) -> None:
    settings = _backup_settings(tmp_path)
    dump = tmp_path / "z.dump"
    dump.write_bytes(b"z")
    with (
        patch("aitrace.ops.backup.load_settings", return_value=settings),
        patch("aitrace.ops.backup.run_restore", side_effect=BackupError("目标库非空或无法确认")),
    ):
        code = console_main(["restore", str(dump), "--force"])
    assert code == 1
    assert "目标库非空" in capsys.readouterr().err


def test_console_missing_config_returns_1(tmp_path, capsys) -> None:
    with patch(
        "aitrace.ops.backup.load_settings", side_effect=SettingsError("配置校验失败，服务拒绝启动")
    ):
        code = console_main(["backup"])
    assert code == 1
    assert "配置校验失败" in capsys.readouterr().err
