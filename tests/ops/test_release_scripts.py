from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "deploy" / "scripts"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
set -e
echo "systemctl $*" >> "$FAKE_CALLS"
case "$1" in
  stop)
    rm -f "$SVC_BROKEN"
    touch "$SVC_DOWN"
    ;;
  start)
    rm -f "$SVC_DOWN"
    if [ -f "$BREAK_FIRST_START" ] && [ ! -f "$STARTED_ONCE" ]; then
      touch "$STARTED_ONCE" "$SVC_BROKEN"
    fi
    ;;
  *) exit 1 ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
[ -f "$SVC_BROKEN" ] && exit 1
[ -f "$SVC_DOWN" ] && exit 1
exit 0
"""

PYTHON_STUB = """#!/usr/bin/env bash
set -e
echo "python3.13 $*" >> "$FAKE_CALLS"
if [ "$1" = "-m" ] && [ "$2" = "venv" ] && [ -n "${3:-}" ]; then
  mkdir -p "$3/bin"
  printf '#!/usr/bin/env bash\\necho "alembic $*" >> "$FAKE_CALLS"\\nexit 0\\n' > "$3/bin/alembic"
  printf '#!/usr/bin/env bash\\necho "pip $*" >> "$FAKE_CALLS"\\nexit 0\\n' > "$3/bin/pip"
  chmod +x "$3/bin/alembic" "$3/bin/pip"
fi
exit 0
"""


def _write_exe(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _make_stub_dir(stubs: Path) -> None:
    stubs.mkdir()
    _write_exe(stubs / "systemctl", SYSTEMCTL_STUB)
    _write_exe(stubs / "curl", CURL_STUB)
    _write_exe(stubs / "python3.13", PYTHON_STUB)


def _install_fake_release(root: Path, revision: str) -> Path:
    (root / "releases").mkdir(parents=True, exist_ok=True)
    target = root / "releases" / revision
    target.mkdir(parents=True)
    (target / "REVISION").write_text(revision, encoding="utf-8")
    (target / "DB_COMPAT").write_text("0001", encoding="utf-8")
    (target / "wheels").mkdir()
    return target


def _new_release_src(base: Path, revision: str) -> Path:
    src = base / f"src-{revision}"
    (src / "wheels").mkdir(parents=True)
    (src / "REVISION").write_text(revision, encoding="utf-8")
    (src / "DB_COMPAT").write_text("0001", encoding="utf-8")
    return src


def _build_env(root: Path, stubs: Path, state: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "AITRACE_INSTALL_ROOT": str(root),
        "AITRACE_SERVICE_NAME": "aitrace.service",
        "AITRACE_UPGRADE_LOG": str(root / "upgrade.log"),
        "AITRACE_READYZ_RETRIES": "1",
        "AITRACE_READYZ_INTERVAL": "0",
        "AITRACE_STOP_WAIT": "0",
        "FAKE_CALLS": str(state / "calls.log"),
        "SVC_DOWN": str(state / "down"),
        "SVC_BROKEN": str(state / "broken"),
        "STARTED_ONCE": str(state / "started_once"),
    }
    env.update(extra)
    return os.environ | env


def _new_root(tmp_path: Path) -> Path:
    root = tmp_path / "opt"
    root.mkdir()
    return root


def _run_script(script: str, args: list[str], root: Path, stubs: Path, state: Path, **extra: str):
    return subprocess.run(
        [str(SCRIPTS / script), *args],
        env=_build_env(root, stubs, state, **extra),
        capture_output=True,
        text=True,
    )


def _upgrade(root: Path, src: Path, stubs: Path, state: Path, **extra: str):
    return _run_script("upgrade.sh", ["--skip-backup-check", str(src)], root, stubs, state, **extra)


def test_upgrade_happy_path_executes_six_phases(tmp_path) -> None:
    root = _new_root(tmp_path)
    v1 = _install_fake_release(root, "1.1.0")
    os.symlink(v1, root / "current")
    src = _new_release_src(tmp_path, "1.2.0")
    stubs = tmp_path / "stubs"
    _make_stub_dir(stubs)
    state = tmp_path / "state"
    state.mkdir()

    proc = _upgrade(root, src, stubs, state)
    assert proc.returncode == 0, proc.stderr

    log = (root / "upgrade.log").read_text(encoding="utf-8")
    marks = [
        "预检通过",
        "安装完成",
        "服务已停止",
        "迁移完成",
        "切换 current",
        "服务已启动",
        "readyz 通过",
    ]
    indexes = [log.index(mark) for mark in marks]
    assert indexes == sorted(indexes)
    assert os.readlink(root / "current") == str(root / "releases" / "1.2.0")
    assert (root / "releases" / "1.2.0" / "alembic.ini").is_file()

    calls = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    stop_i = next(i for i, c in enumerate(calls) if c == "systemctl stop aitrace.service")
    alembic_i = next(i for i, c in enumerate(calls) if c.startswith("alembic"))
    start_i = next(i for i, c in enumerate(calls) if c == "systemctl start aitrace.service")
    assert stop_i < alembic_i < start_i


def test_upgrade_rolls_back_on_readyz_failure(tmp_path) -> None:
    root = _new_root(tmp_path)
    v1 = _install_fake_release(root, "1.1.0")
    os.symlink(v1, root / "current")
    src = _new_release_src(tmp_path, "1.2.0")
    stubs = tmp_path / "stubs"
    _make_stub_dir(stubs)
    state = tmp_path / "state"
    state.mkdir()
    (state / "break").touch()

    proc = _upgrade(root, src, stubs, state, BREAK_FIRST_START=str(state / "break"))
    assert proc.returncode == 1
    log = (root / "upgrade.log").read_text(encoding="utf-8")
    assert "升级失败，已回滚" in log
    assert "回滚完成" in log
    assert os.readlink(root / "current") == str(v1)
    assert (root / "releases" / "1.2.0").is_dir()

    calls = (state / "calls.log").read_text(encoding="utf-8").splitlines()
    starts = [c for c in calls if c == "systemctl start aitrace.service"]
    assert len(starts) == 2


def test_retention_keeps_three_releases(tmp_path) -> None:
    root = _new_root(tmp_path)
    for revision in ("1.1.0", "1.2.0", "1.3.0", "1.4.0"):
        _install_fake_release(root, revision)
    os.symlink(root / "releases" / "1.4.0", root / "current")
    src = _new_release_src(tmp_path, "1.5.0")
    stubs = tmp_path / "stubs"
    _make_stub_dir(stubs)
    state = tmp_path / "state"
    state.mkdir()

    proc = _upgrade(root, src, stubs, state)
    assert proc.returncode == 0, proc.stderr
    remaining = sorted(p.name for p in (root / "releases").iterdir())
    assert remaining == ["1.3.0", "1.4.0", "1.5.0"]
    assert os.readlink(root / "current") == str(root / "releases" / "1.5.0")


def test_rollback_missing_target_fails_with_chinese_message(tmp_path) -> None:
    root = _new_root(tmp_path)
    stubs = tmp_path / "stubs"
    _make_stub_dir(stubs)
    state = tmp_path / "state"
    state.mkdir()
    proc = _run_script("rollback.sh", [str(tmp_path / "nope")], root, stubs, state)
    assert proc.returncode == 1
    assert "目标版本目录不存在" in proc.stderr
