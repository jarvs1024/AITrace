# src/aitrace/probe/client.py
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from re import Pattern

from aitrace.core.audit import AuditService
from aitrace.observability.metrics import MetricsRegistry
from aitrace.probe.whitelist import DENY_PATTERNS, GLOBAL_PROBE_WHITELIST

Runner = Callable[[str, str], tuple[int, str, str]]


class ProbeDenied(ValueError):
    code = "PROBE_FORBIDDEN"


class ProbeTimeout(RuntimeError):
    code = "PROBE_TIMEOUT"


@dataclass(frozen=True)
class ProbeResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    at: datetime


def build_ssh_runner(*, connect_timeout: int = 5, command_timeout: int = 10) -> Runner:
    def run(host: str, command: str) -> tuple[int, str, str]:
        completed = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={connect_timeout}",
                host,
                command,
            ],
            capture_output=True,
            text=True,
            timeout=command_timeout,
            check=False,
        )
        return completed.returncode, completed.stdout, completed.stderr

    return run


class ProbeClient:
    """SSH 只读探针：全局白名单 ∩ 画像白名单，逐次审计哈希链。

    匹配语义：全局白名单为锚定正则（re.fullmatch），是最终安全闸；
    画像白名单按前缀语义（命令本身或以「条目 + 空格」开头），约束域可用命令面。
    """

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        global_whitelist: list[str] | None = None,
        audit: AuditService,
        profile_whitelist: list[str] | None = None,
        connect_timeout: int = 5,
        command_timeout: int = 10,
        registry: MetricsRegistry | None = None,
    ) -> None:
        self._runner = runner or build_ssh_runner(
            connect_timeout=connect_timeout, command_timeout=command_timeout
        )
        patterns = (
            global_whitelist if global_whitelist is not None else list(GLOBAL_PROBE_WHITELIST)
        )
        self._global: list[Pattern[str]] = [re.compile(pattern) for pattern in patterns]
        self._deny: list[Pattern[str]] = [re.compile(pattern) for pattern in DENY_PATTERNS]
        self._profile = list(profile_whitelist or [])
        self._audit = audit
        self._registry = registry

    def _record(self, ok: bool) -> None:
        if self._registry is None:
            return
        key = "probe_ok" if ok else "probe_fail"
        self._registry.counter(f"{key}_total").inc()
        if ok:
            self._registry.gauge("probe_age_seconds").set(0.0)
        else:
            self._registry.window_counter(key).inc()

    def run(self, *, host: str, command: str, session_id: str) -> ProbeResult:
        command = command.strip()
        deny = next((pattern for pattern in self._deny if pattern.search(command)), None)
        if deny is not None:
            self._record(ok=False)
            self._audit_denied(host, command, session_id, f"命中禁用模式 {deny.pattern!r}")
            raise ProbeDenied(
                "探针命令被拒绝（PROBE_FORBIDDEN）："
                f"当前命令 {command!r}；"
                f"原因：命中禁用模式 {deny.pattern!r}；"
                "要求：仅允许只读诊断命令"
            )
        if not any(pattern.fullmatch(command) for pattern in self._global):
            self._record(ok=False)
            self._audit_denied(host, command, session_id, "不在全局白名单")
            raise ProbeDenied(
                "探针命令被拒绝（PROBE_FORBIDDEN）："
                f"当前命令 {command!r}；"
                "原因：命令不在全局白名单；"
                "要求：命令必须完全匹配全局只读白名单正则之一"
            )
        profile_ok = self._profile and any(
            command == entry or command.startswith(entry + " ") for entry in self._profile
        )
        if not profile_ok:
            allowed = "、".join(self._profile) or "<空>"
            self._record(ok=False)
            self._audit_denied(host, command, session_id, f"不在画像白名单（{allowed}）")
            raise ProbeDenied(
                "探针命令被拒绝（PROBE_FORBIDDEN）："
                f"当前命令 {command!r}；"
                f"原因：命令不在画像白名单；画像允许的命令前缀：{allowed}；"
                "要求：命令需同时命中全局白名单与画像白名单（交集非空）"
            )
        try:
            exit_code, stdout, stderr = self._runner(host, command)
        except (subprocess.TimeoutExpired, TimeoutError):
            self._record(ok=False)
            self._audit.append(
                actor="probe",
                action="SSH_PROBE_TIMEOUT",
                target=host,
                detail={"command": command, "session_id": session_id},
            )
            raise ProbeTimeout(
                "探针命令执行超时（PROBE_TIMEOUT）："
                f"当前命令 {command!r}，主机 {host}；"
                "原因：SSH 执行超过命令超时上限；要求：检查主机可达性或缩小命令范围"
            ) from None
        self._audit.append(
            actor="probe",
            action="SSH_PROBE",
            target=host,
            detail={
                "command": command,
                "session_id": session_id,
                "rc": int(exit_code),
                "bytes": len(stdout or ""),
            },
        )
        self._record(ok=exit_code == 0)
        return ProbeResult(
            command=command,
            exit_code=int(exit_code),
            stdout=stdout or "",
            stderr=stderr or "",
            at=datetime.now(UTC),
        )

    def _audit_denied(self, host: str, command: str, session_id: str, reason: str) -> None:
        self._audit.append(
            actor="probe",
            action="SSH_PROBE_DENIED",
            target=host,
            detail={
                "command": command,
                "session_id": session_id,
                "reason": reason,
            },
        )
