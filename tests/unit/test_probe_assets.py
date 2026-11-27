from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from aitrace.core.audit import AuditService
from aitrace.probe.assets import host_in_assets
from aitrace.probe.client import ProbeClient, ProbeDenied

_ALLOWLIST = ["10.0.0.0/24", "192.168.5.7"]


class TestHostInAssets:
    def test_exact_ip_in_allowlist(self) -> None:
        assert host_in_assets("192.168.5.7", _ALLOWLIST, allow_loopback=True) is True

    def test_ip_in_cidr(self) -> None:
        assert host_in_assets("10.0.0.13", _ALLOWLIST, allow_loopback=False) is True

    def test_ip_outside_denied(self) -> None:
        assert host_in_assets("10.6.6.6", _ALLOWLIST, allow_loopback=True) is False

    def test_loopback_respects_flag(self) -> None:
        assert host_in_assets("127.0.0.1", [], allow_loopback=True) is True
        assert host_in_assets("127.0.0.2", [], allow_loopback=True) is True
        assert host_in_assets("127.0.0.1", [], allow_loopback=False) is False
        assert host_in_assets("::1", [], allow_loopback=False) is False

    def test_hostname_always_denied(self) -> None:
        assert host_in_assets("ssd-lab-01.internal", _ALLOWLIST, allow_loopback=True) is False
        assert host_in_assets("localhost", _ALLOWLIST, allow_loopback=True) is False

    def test_cidr_form_host_denied(self) -> None:
        assert host_in_assets("10.0.0.1/24", _ALLOWLIST, allow_loopback=True) is False

    def test_garbage_denied(self) -> None:
        for bad in ("", "not-an-ip", "10.0.0.999"):
            assert host_in_assets(bad, _ALLOWLIST, allow_loopback=True) is False


@pytest.fixture(autouse=True)
def _isolate_audit_log(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE audit_log RESTART IDENTITY"))


@pytest.fixture
def audit(db_engine: Engine) -> AuditService:
    return AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))


def _sentinel(host: str, command: str) -> tuple[int, str, str]:
    raise AssertionError(f"runner 不应被调用：{host} {command}")


class TestProbeHostGate:
    def test_host_outside_assets_denied_before_runner(self, audit: AuditService) -> None:
        client = ProbeClient(
            runner=_sentinel,
            audit=audit,
            profile_whitelist=["nvme list"],
            host_allowlist=["10.0.0.0/24"],
        )
        with pytest.raises(ProbeDenied) as caught:
            client.run(host="45.32.1.8", command="nvme list", session_id="s-1")
        assert caught.value.code == "PROBE_FORBIDDEN"
        assert "45.32.1.8" in str(caught.value)
        assert "资产范围" in str(caught.value)

    def test_host_gate_audits_deny_with_host(self, audit: AuditService, db_engine: Engine) -> None:
        client = ProbeClient(
            runner=_sentinel,
            audit=audit,
            profile_whitelist=["nvme list"],
            host_allowlist=["10.0.0.0/24"],
        )
        with pytest.raises(ProbeDenied):
            client.run(host="45.32.1.8", command="reboot", session_id="s-2")
        with sessionmaker(bind=db_engine)() as session:
            rows = (
                session.execute(text("SELECT action, detail FROM audit_log ORDER BY id"))
                .mappings()
                .all()
            )
        assert rows[-1]["action"] == "SSH_PROBE_DENIED"
        assert rows[-1]["detail"]["reason"].startswith("不在主机资产范围")

    def test_unconfigured_allowlist_denies_non_loopback(self, audit: AuditService) -> None:
        client = ProbeClient(runner=_sentinel, audit=audit, profile_whitelist=["nvme list"])
        with pytest.raises(ProbeDenied):
            client.run(host="10.0.0.1", command="nvme list", session_id="s-3")

    def test_loopback_allowed_without_allowlist(self, audit: AuditService) -> None:
        client = ProbeClient(
            runner=lambda host, command: (0, "ok", ""),
            audit=audit,
            profile_whitelist=["nvme list"],
        )
        result = client.run(host="127.0.0.1", command="nvme list", session_id="s-4")
        assert result.exit_code == 0

    def test_host_in_allowlist_proceeds(self, audit: AuditService) -> None:
        client = ProbeClient(
            runner=lambda host, command: (0, "ok", ""),
            audit=audit,
            profile_whitelist=["nvme list"],
            host_allowlist=["10.0.0.0/24"],
        )
        result = client.run(host="10.0.0.1", command="nvme list", session_id="s-5")
        assert result.exit_code == 0
