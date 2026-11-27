from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from tests.conftest import requires_db

from aitrace.core.audit import AuditService
from aitrace.probe.client import ProbeClient, ProbeDenied

pytestmark = requires_db


@pytest.fixture(autouse=True)
def _isolate_audit_log(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))


@pytest.fixture
def audit(db_engine: Engine) -> AuditService:
    return AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))


def _sentinel(host: str, command: str) -> tuple[int, str, str]:
    raise AssertionError(f"runner 不应被调用：{host} {command}")


@pytest.mark.parametrize(
    "command",
    [
        "nvme list\nreboot",
        "nvme list；rm -rf /",
        "nvme li$(rm)st",
        "nvme list `id`",
        "cxl list --format json",
        "nvme smart-log /dev/nvme0n1;",
        "nvme list -o json | cat",
    ],
)
def test_shell_metachar_or_unknown_option_denied(audit: AuditService, command: str) -> None:
    client = ProbeClient(
        runner=_sentinel,
        audit=audit,
        profile_whitelist=["nvme list", "nvme smart-log", "cxl list"],
        host_allowlist=["10.0.0.0/24"],
    )
    with pytest.raises(ProbeDenied) as caught:
        client.run(host="10.0.0.1", command=command, session_id="s-1")
    assert caught.value.code == "PROBE_FORBIDDEN"
    # 拒绝消息按 {command!r} 回显当前命令（红队校验：当前值必带），
    # 故以 repr 比对——含换行等转义字符的命令在消息中同样以转义形式呈现。
    assert repr(command) in str(caught.value)


def test_cloud_metadata_host_denied_ssrf_red_team(audit: AuditService) -> None:
    client = ProbeClient(
        runner=_sentinel,
        audit=audit,
        profile_whitelist=["nvme list"],
        host_allowlist=["10.0.0.0/24"],
    )
    with pytest.raises(ProbeDenied) as caught:
        client.run(host="169.254.169.254", command="nvme list", session_id="s-2")
    assert "169.254.169.254" in str(caught.value)


def test_deny_paths_never_invoke_runner(audit: AuditService) -> None:
    client = ProbeClient(
        runner=_sentinel,
        audit=audit,
        profile_whitelist=["nvme list"],
        host_allowlist=["10.0.0.0/24"],
    )
    for host, command in [
        ("45.32.1.8", "nvme list"),
        ("10.0.0.1", "reboot"),
        ("10.0.0.1", "nvme format /dev/nvme0n1"),
    ]:
        with pytest.raises(ProbeDenied):
            client.run(host=host, command=command, session_id="s-3")
