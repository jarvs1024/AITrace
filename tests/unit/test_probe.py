# tests/unit/test_probe.py
from __future__ import annotations

import subprocess
import time
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from aitrace.core.audit import AuditService
from aitrace.probe.client import ProbeClient, ProbeDenied, ProbeResult, ProbeTimeout

_ALLOWED = (
    "nvme list",
    "nvme id-ctrl /dev/nvme0n1",
    "nvme smart-log /dev/nvme0n1",
    "nvme error-log /dev/nvme0n1",
    "cxl list",
    "cxl list -v",
    "lspci -vvv -s 01:00.0",
    "lspci -vv",
    "smartctl -a /dev/nvme0n1",
    "dmesg --read",
    "df -h /data",
    "ps -ef | grep nvme",
)


@pytest.fixture(autouse=True)
def _isolate_audit_log(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))


@pytest.fixture
def audit(db_engine: Engine) -> AuditService:
    return AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))


@pytest.fixture
def client(audit: AuditService) -> ProbeClient:
    return ProbeClient(
        runner=lambda host, command: (0, "stdout 正常", ""),
        audit=audit,
        profile_whitelist=[
            "nvme list",
            "nvme id-ctrl",
            "nvme smart-log",
            "nvme error-log",
            "cxl list",
            "lspci",
            "smartctl -a",
            "dmesg",
            "df -h",
            "ps -ef | grep",
        ],
    )


def _sentinel_runner(host: str, command: str) -> tuple[int, str, str]:
    return (0, f"ran {command} on {host}", "")


class TestWhitelistDeny:
    @pytest.mark.parametrize(
        "command",
        [
            "reboot",
            "sudo reboot",
            "shutdown -h now",
            "nvme format /dev/nvme0n1",
            "nvme fw-download -f fw.bin",
            "nvme sanitize /dev/nvme0n1",
            "rm -rf /tmp/x",
            "nvme list | reboot",
            "nvme list; cat /etc/shadow",
            "nvme list > /tmp/out.txt",
        ],
    )
    def test_destructive_or_injected_commands_denied(
        self, client: ProbeClient, command: str
    ) -> None:
        with pytest.raises(ProbeDenied) as caught:
            client.run(host="10.0.0.1", command=command, session_id="s-1")
        assert caught.value.code == "PROBE_FORBIDDEN"
        assert command in str(caught.value)


class TestWhitelistAllow:
    @pytest.mark.parametrize("command", _ALLOWED)
    def test_readonly_commands_allowed(self, client: ProbeClient, command: str) -> None:
        result = client.run(host="10.0.0.1", command=command, session_id="s-1")
        assert isinstance(result, ProbeResult)
        assert result.exit_code == 0
        assert result.command == command


class TestProfileIntersection:
    def test_empty_profile_denies_all(self, audit: AuditService) -> None:
        client = ProbeClient(runner=_sentinel_runner, audit=audit, profile_whitelist=[])
        with pytest.raises(ProbeDenied) as caught:
            client.run(host="10.0.0.1", command="nvme list", session_id="s-1")
        assert caught.value.code == "PROBE_FORBIDDEN"

    def test_profile_none_treated_as_empty(self, audit: AuditService) -> None:
        client = ProbeClient(runner=_sentinel_runner, audit=audit, profile_whitelist=None)
        with pytest.raises(ProbeDenied):
            client.run(host="10.0.0.1", command="nvme list", session_id="s-1")

    def test_command_outside_profile_denied(self, audit: AuditService) -> None:
        client = ProbeClient(
            runner=_sentinel_runner,
            audit=audit,
            profile_whitelist=["nvme list", "smartctl -a"],
        )
        with pytest.raises(ProbeDenied):
            client.run(host="10.0.0.1", command="cxl list", session_id="s-1")

    def test_profile_prefix_allows_arguments(self, audit: AuditService) -> None:
        client = ProbeClient(
            runner=_sentinel_runner,
            audit=audit,
            profile_whitelist=["nvme id-ctrl", "nvme list"],
        )
        result = client.run(host="10.0.0.1", command="nvme id-ctrl /dev/nvme0n1", session_id="s-1")
        assert result.exit_code == 0


class TestAuditAndResult:
    def test_probe_run_audited_and_hash_chain_verifies(
        self, audit: AuditService, client: ProbeClient, db_engine: Engine
    ) -> None:
        result = client.run(host="10.0.0.1", command="nvme list", session_id="s-1")
        assert result.stdout == "stdout 正常"
        assert audit.verify_chain() is True
        with sessionmaker(bind=db_engine)() as session:
            row = (
                session.execute(
                    text(
                        "SELECT action, target, detail FROM audit_log "
                        "WHERE actor = 'probe' ORDER BY id"
                    )
                )
                .mappings()
                .all()
            )
        assert len(row) == 1
        assert row[0]["action"] == "SSH_PROBE"
        assert row[0]["target"] == "10.0.0.1"
        detail: dict[str, Any] = row[0]["detail"]
        assert detail["command"] == "nvme list"
        assert detail["session_id"] == "s-1"
        assert detail["rc"] == 0
        assert detail["bytes"] == len("stdout 正常")

    def test_denied_probe_is_audited(self, audit: AuditService, db_engine: Engine) -> None:
        client = ProbeClient(runner=_sentinel_runner, audit=audit, profile_whitelist=["nvme list"])
        with pytest.raises(ProbeDenied):
            client.run(host="10.0.0.1", command="reboot", session_id="s-2")
        assert audit.verify_chain() is True
        with sessionmaker(bind=db_engine)() as session:
            row = (
                session.execute(text("SELECT action, detail FROM audit_log ORDER BY id"))
                .mappings()
                .all()
            )
        assert row[-1]["action"] == "SSH_PROBE_DENIED"
        assert row[-1]["detail"]["command"] == "reboot"

    def test_runner_timeout_raises_and_is_audited(
        self, audit: AuditService, db_engine: Engine
    ) -> None:
        def slow_runner(host: str, command: str) -> tuple[int, str, str]:
            raise subprocess.TimeoutExpired(cmd=command, timeout=10)

        client = ProbeClient(
            runner=slow_runner,
            audit=audit,
            profile_whitelist=["nvme list"],
        )
        with pytest.raises(ProbeTimeout) as caught:
            client.run(host="10.0.0.1", command="nvme list", session_id="s-3")
        assert caught.value.code == "PROBE_TIMEOUT"
        with sessionmaker(bind=db_engine)() as session:
            row = (
                session.execute(text("SELECT action, detail FROM audit_log ORDER BY id"))
                .mappings()
                .all()
            )
        assert row[-1]["action"] == "SSH_PROBE_TIMEOUT"

    def test_runner_raises_timeout_error_also_probe_timeout(self, audit: AuditService) -> None:
        def timeouter(host: str, command: str) -> tuple[int, str, str]:
            raise TimeoutError("死等")

        client = ProbeClient(runner=timeouter, audit=audit, profile_whitelist=["nvme list"])
        with pytest.raises(ProbeTimeout):
            client.run(host="10.0.0.1", command="nvme list", session_id="s-4")

    def test_result_carries_stderr_and_timestamp(self, client: ProbeClient) -> None:
        before = time.time()
        result = client.run(host="10.0.0.1", command="cxl list", session_id="s-5")
        after = time.time()
        assert result.stderr == ""
        assert result.stdout == "stdout 正常"
        assert result.command == "cxl list"
        assert before <= result.at.timestamp() <= after
