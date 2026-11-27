# src/aitrace/ops/startup.py
"""启动自检五查（fail-fast）与凭据文件扫描。

任一问题汇总为 StartupError：中文逐条列出问题与「建议」修复行；
凭据扫描只回显文件名，绝不回显内容。错误消息不出现令牌原文。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.settings import Settings
from aitrace.storage.watermark import WatermarkLevel, disk_fraction, level_for

logger = logging.getLogger(__name__)

_SUBDIRS = ("evidence", "rawlogs", "runtime", "memory", "logs")
_NAME_PATTERNS = ("token", "credential", ".pem", "id_rsa", "qoder", ".key", ".p12", ".ppk")
_CONTENT_MARKER = b"BEGIN PRIVATE KEY"
_MAX_SCAN_BYTES = 1024 * 1024


class StartupError(RuntimeError):
    """启动自检失败；message 为中文问题清单。"""


def _name_hit(name: str) -> bool:
    lowered = name.lower()
    return any(pattern in lowered for pattern in _NAME_PATTERNS)


def _content_hit(path: Path) -> bool:
    try:
        head = path.read_bytes()[:_MAX_SCAN_BYTES]
    except OSError:
        return False
    return _CONTENT_MARKER in head


def scan_for_credential_files(
    *, runtime_dir: Path, home_dir: Path | None = None
) -> tuple[list[str], list[str]]:
    """返回 (runtime 命中绝对路径列表, HOME 命中列表)。

    runtime 目录全递归（文件名模式 + 私钥内容标记）；HOME 仅扫顶层文件名
    （开发机普遍存在 ~/.ssh，递归扫描的误报不进失败清单）。
    """
    runtime_hits: list[str] = []
    if runtime_dir.is_dir():
        for root, dirs, files in os.walk(runtime_dir):
            dirs.sort()
            for name in sorted(files):
                path = Path(root) / name
                if _name_hit(name) or _content_hit(path):
                    runtime_hits.append(str(path))
    home_hits: list[str] = []
    if home_dir is not None and home_dir.is_dir():
        try:
            entries = list(home_dir.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if path.is_file() and _name_hit(path.name):
                home_hits.append(str(path))
    return runtime_hits, home_hits


def run_startup_checks(settings: Settings, session_factory: sessionmaker[Session]) -> None:
    problems: list[str] = []

    if settings.service_token is None:
        problems.append(
            "服务令牌（service_token）：未配置；要求：设置 AITRACE_SERVICE_TOKEN "
            "或 AITRACE_SERVICE_TOKEN_FILE；建议：补齐配置后重启"
        )

    data_dir = settings.storage.data_dir
    if not data_dir.is_dir():
        problems.append(
            f"数据根目录（storage.data_dir）：{data_dir} 不存在；"
            "建议：创建该目录并确保运行账号可写后重启（自检不自动创建数据根目录）"
        )
    else:
        for sub in _SUBDIRS:
            try:
                (data_dir / sub).mkdir(parents=True, exist_ok=True)
            except OSError:
                problems.append(
                    f"数据子目录：{data_dir / sub} 无法创建；建议：检查磁盘空间与目录权限"
                )
        home = os.environ.get("HOME")
        if not home or not Path(home).is_dir():
            problems.append(
                f"家目录：HOME={home or '<未设置>'} 不存在；建议：设置有效的 HOME 环境变量"
            )
        elif not os.access(home, os.W_OK):
            problems.append(f"家目录：{home} 不可写；建议：检查运行账号权限")

    try:
        fraction = disk_fraction(data_dir, data_dir / "rawlogs", settings.storage.l2_quota_bytes)
    except OSError:
        fraction = 0.0
    if (
        level_for(
            fraction,
            settings.storage.watermark_warn_fraction,
            settings.storage.watermark_force_fraction,
        )
        is WatermarkLevel.FORCE
    ):
        problems.append(
            f"磁盘水位：已进入强制清理档（当前占用 {fraction:.1%}）；"
            "建议：清理 data/rawlogs 或调低 storage.l2_quota_bytes 后重启"
        )

    runtime_hits, home_hits = scan_for_credential_files(
        runtime_dir=data_dir / "runtime",
        home_dir=Path(os.environ["HOME"]) if os.environ.get("HOME") else None,
    )
    if runtime_hits:
        problems.append(
            "疑似凭据文件落在运行时目录："
            + "、".join(runtime_hits)
            + "；建议：立即移出运行目录，凭据严禁落盘（仅经配置或环境变量注入）"
        )
    for hit in home_hits:
        logger.warning("家目录顶层发现疑似凭据文件（不阻断启动）：%s", hit)

    db_rev: str | None = None
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            try:
                count = session.execute(text("SELECT count(*) FROM alembic_version")).scalar_one()
            except Exception:
                problems.append(
                    "数据库迁移版本表：alembic_version 不存在或不可读；"
                    "建议：执行 alembic upgrade head 完成迁移"
                )
            else:
                if count == 1:
                    db_rev = str(
                        session.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalar_one()
                    )
                else:
                    problems.append(
                        f"数据库迁移版本表：alembic_version 行数异常（当前 {count}，要求 1）；"
                        "建议：检查迁移版本表或执行 alembic upgrade head"
                    )
    except Exception:
        problems.append("数据库连通性：连接失败；建议：检查 database.dsn 配置与数据库服务状态")

    release_root = settings.release_root
    if release_root is not None:
        revision_file = release_root / "REVISION"
        if not revision_file.is_file():
            problems.append(f"发布目录：缺少版本文件 {revision_file}；建议：补齐发布物料 REVISION")
        compat_file = release_root / "DB_COMPAT"
        if compat_file.is_file():
            compat = compat_file.read_text(encoding="utf-8").strip()
            if db_rev is None:
                problems.append(
                    f"数据库迁移版本表异常，无法核对 DB_COMPAT（本发布要求 {compat}）；"
                    "建议：先修复 alembic_version 再核对发布兼容性"
                )
            else:
                try:
                    db_num, compat_num = int(db_rev), int(compat)
                except ValueError:
                    problems.append(
                        f"数据库迁移版本表：revision（当前 {db_rev}）"
                        f"或 DB_COMPAT（当前 {compat}）不是数字；建议：检查版本内容"
                    )
                else:
                    if db_num < compat_num:
                        problems.append(
                            f"数据库 revision（当前 {db_rev}）落后于本发布要求（{compat}）；"
                            "建议：先执行数据库迁移升级，再启动本版本"
                        )
                    elif db_num > compat_num:
                        problems.append(
                            f"数据库 revision（当前 {db_rev}）领先于本发布要求（{compat}）；"
                            "建议：确认是否连错数据库，或回滚至对应发布版本"
                        )
        qoder_bin = release_root / "bin" / "qoder"
        if not (qoder_bin.is_file() and os.access(qoder_bin, os.X_OK)):
            problems.append(
                f"运维二进制：{qoder_bin} 不存在或不可执行；建议：检查发布目录 bin/qoder"
            )

    if problems:
        raise StartupError("启动自检失败，服务拒绝启动：\n- " + "\n- ".join(problems))
