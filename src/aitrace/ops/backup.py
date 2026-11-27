# src/aitrace/ops/backup.py
"""aitrace-backup CLI：数据库备份（pg_dump 自定义格式）、SHA-256 校验与恢复演练。

密码只经子进程环境变量 PGPASSWORD 传递，绝不进 argv/日志；
目标库非空时恢复默认拒绝（--force 覆盖），供 RPO/RTO 演练。
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine.url import make_url

from aitrace.core.settings import Settings, SettingsError, load_settings

logger = logging.getLogger(__name__)

_DUMP_RE = re.compile(r"^aitrace-\d{8}-\d{6}\.dump$")


class BackupError(RuntimeError):
    """备份/恢复失败；message 中文，含建议。"""


class SpaceShortageError(BackupError):
    """备份暂存空间不足；CLI 退出码 3，不产生 dump。"""


@dataclass
class BackupResult:
    path: Path
    digest: str
    size_bytes: int
    pushed: bool


def _pg_env_parts(dsn: str) -> tuple[str, dict[str, str]]:
    """拆 DSN 为 (libpq 连接串, 子进程环境)；密码只入 env。"""
    url = make_url(dsn)
    env: dict[str, str] = {}
    if url.password:
        env["PGPASSWORD"] = url.password
    driver = url.drivername.replace("+psycopg", "", 1)
    username = f"{url.username}@" if url.username else ""
    host = url.host or ""
    port = f":{url.port}" if url.port else ""
    database = f"/{url.database}" if url.database else ""
    conn = f"{driver}://{username}{host}{port}{database}"
    return conn, env


def _run_silent(cmd: list[str], env: dict[str, str]) -> None:
    merged = os.environ | env
    result = subprocess.run(cmd, capture_output=True, text=True, env=merged)
    if result.returncode != 0:
        raise BackupError(f"命令失败（返回码 {result.returncode}）：{' '.join(cmd)}")


def _retain_backups(staging: Path, keep: int) -> list[Path]:
    """按文件名时间序保留最近 keep 份，同删 .digest；返回被删列表。"""
    dumps = sorted(
        (p for p in staging.glob("*.dump") if _DUMP_RE.match(p.name)),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    for old in dumps[: max(0, len(dumps) - keep)]:
        old.unlink(missing_ok=True)
        (staging / (old.name + ".digest")).unlink(missing_ok=True)
        removed.append(old)
    return removed


def _push(target: Path, digest_file: Path, remote_dest: str) -> None:
    if "://" not in remote_dest and not remote_dest.startswith("rsync"):
        dest_dir = Path(remote_dest)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest_dir / target.name)
        shutil.copy2(digest_file, dest_dir / digest_file.name)
        return
    subprocess.run(
        ["rsync", "-a", str(target), str(digest_file), remote_dest.rstrip("/") + "/"],
        check=True,
        capture_output=True,
        text=True,
    )


def run_backup(
    settings: Settings,
    *,
    staging: Path | None = None,
    keep: int | None = None,
    now: datetime | None = None,
) -> BackupResult:
    backup = settings.backup
    staging = staging or backup.staging_dir
    keep = keep or backup.keep_count
    now = now or datetime.now(UTC)
    staging.mkdir(parents=True, exist_ok=True)

    free = shutil.disk_usage(staging).free
    if free < backup.min_free_bytes:
        raise SpaceShortageError(
            f"可用空间不足：备份暂存目录 {staging} 当前可用 {free} 字节，"
            f"低于要求 {backup.min_free_bytes} 字节；跳过本次备份"
        )

    _retain_backups(staging, keep)

    conn, env = _pg_env_parts(settings.database.dsn)
    target = staging / f"aitrace-{now:%Y%m%d-%H%M%S}.dump"
    try:
        _run_silent(
            ["pg_dump", "-Fc", "--no-password", f"--dbname={conn}", "-f", str(target)],
            env,
        )
    except BackupError as exc:
        target.unlink(missing_ok=True)
        raise BackupError(f"数据库备份失败：{exc}") from exc
    try:
        _run_silent(["pg_restore", "--list", str(target)], env)
    except BackupError as exc:
        raise BackupError(
            f"备份清单校验失败（dump 已保留待人工核查）：{target}；原因：{exc}"
        ) from exc

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    digest_file = staging / (target.name + ".digest")
    digest_file.write_text(f"{digest}  {target.name}\n", encoding="utf-8")

    pushed = False
    if backup.remote_dest:
        try:
            _push(target, digest_file, backup.remote_dest)
            pushed = True
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(
                "异盘推送失败，本地备份保留：%s -> %s（%s）", target, backup.remote_dest, exc
            )
    return BackupResult(path=target, digest=digest, size_bytes=target.stat().st_size, pushed=pushed)


def run_restore(
    dump: Path,
    settings: Settings,
    *,
    dsn: str | None = None,
    force: bool = False,
    engine_factory: Callable[[], Engine] | None = None,
) -> None:
    if not dump.is_file():
        raise BackupError(f"备份文件不存在：{dump}；建议：检查路径或先在备份目录执行 backup")
    dsn = dsn or settings.database.dsn

    if not force:
        factory = engine_factory if engine_factory is not None else lambda: create_engine(dsn)
        engine = factory()
        rows: int | str = "未知"
        try:
            with engine.connect() as db_conn:
                rows = int(db_conn.execute(text("SELECT count(*) FROM task_queue")).scalar_one())
        except Exception:
            rows = "未知"
        finally:
            engine.dispose()
        if rows != 0:
            raise BackupError(
                f"目标库非空或无法确认（task_queue 行数 {rows}），恢复将覆盖现有数据；"
                "建议：确认目标为演练库后加 --force 重试"
            )

    conn, env = _pg_env_parts(dsn)
    try:
        _run_silent(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                f"--dbname={conn}",
                str(dump),
            ],
            env,
        )
    except BackupError as exc:
        raise BackupError(f"恢复失败：{exc}") from exc


def console_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aitrace-backup", description="AITrace 数据库备份/恢复运维 CLI"
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="YAML 配置文件路径（缺省读环境变量）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    backup_p = sub.add_parser("backup", help="执行数据库备份（保留策略 + digest + 异盘推送）")
    backup_p.add_argument("--staging", type=Path, default=None, help="备份暂存目录（覆盖配置）")
    backup_p.add_argument("--keep", type=int, default=None, help="保留份数（覆盖配置）")
    restore_p = sub.add_parser("restore", help="恢复备份（默认拒绝非空目标库）")
    restore_p.add_argument("dump", type=Path, help="备份文件路径")
    restore_p.add_argument("--force", action="store_true", help="允许覆盖非空目标库")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
    except SettingsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "backup":
        try:
            result = run_backup(settings, staging=args.staging, keep=args.keep)
        except SpaceShortageError as exc:
            logger.warning("%s", exc)
            print(str(exc), file=sys.stderr)
            return 3
        except (BackupError, OSError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"备份完成：{result.path}（{result.size_bytes} 字节，SHA-256 {result.digest}）")
        return 0

    try:
        run_restore(args.dump, settings, force=args.force)
    except (BackupError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"恢复完成：{args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(console_main())
