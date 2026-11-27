from __future__ import annotations

import ipaddress
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceInfo(_StrictModel):
    device_id: str = Field(min_length=1)
    pcie_bdf: str = Field(min_length=1)
    device_node: str = Field(min_length=1)
    model: str = Field(min_length=1)
    sn: str = Field(min_length=1)
    fw_version: str = Field(min_length=1)
    slot: str = Field(min_length=1)


class EnvTopology(_StrictModel):
    host_ip: str
    host_boot_time_utc: int = Field(gt=0)
    devices: list[DeviceInfo] = Field(min_length=1)

    @field_validator("host_ip")
    @classmethod
    def _valid_ipv4(cls, value: str) -> str:
        ipaddress.IPv4Address(value)
        return value


class GitContext(_StrictModel):
    repo_url: str = Field(min_length=1)
    commit_id: str = Field(min_length=1)


class LogPaths(_StrictModel):
    test_log_path: str | None = None
    uart_log_path: str | None = None
    kernel_log_path: str | None = None
    scheduler_log_path: str | None = None


class FailedCase(_StrictModel):
    case_id: str = Field(min_length=1)
    script_path: str = Field(min_length=1)
    failed_at_utc: int = Field(gt=0)
    env_binding: dict[str, str]
    logs: LogPaths


class PassedCase(_StrictModel):
    case_id: str = Field(min_length=1)
    script_path: str = Field(min_length=1)


class DeliveryPayload(_StrictModel):
    product_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    trigger_user_id: str = Field(min_length=1)
    hardware_type: Literal["SSD", "CXL"]
    env_topology: EnvTopology
    git_context: GitContext
    failed_cases: list[FailedCase] = Field(min_length=1)
    passed_cases: list[PassedCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_cases(self) -> Self:
        device_ids = {device.device_id for device in self.env_topology.devices}
        seen: set[str] = set()
        for case in self.failed_cases:
            if case.case_id in seen:
                raise ValueError(f"失败用例重复：{case.case_id}")
            seen.add(case.case_id)
            if case.env_binding.get("device_id") not in device_ids:
                raise ValueError(f"用例 {case.case_id} 的 env_binding.device_id 不在设备清单中")
            if not any(case.logs.model_dump().values()):
                raise ValueError(f"用例 {case.case_id} 未提供任何日志路径")
        return self


class SessionResult(_StrictModel):
    case_id: str
    session_id: str
    is_existing: bool
    status: Literal["ACCEPTED", "EXISTING"]


class RejectedCase(_StrictModel):
    case_id: str
    code: str
    message: str
    retryable: bool
    retry_after: int | None = None


class DeliveryResponse(_StrictModel):
    accepted: bool
    batch_task_id: str
    sessions: list[SessionResult]
    rejected: list[RejectedCase] = Field(default_factory=list)


class SessionStatusResponse(_StrictModel):
    session_id: str
    batch_id: str
    case_id: str
    product_id: str
    hardware_type: str
    domain_id: str
    profile_version: str
    round_no: int
    stage: str
    progress: int | None
    waiting_reason: str | None
    message: str
    report: dict[str, Any] | None = None
    citation: dict[str, Any] | None = None
    rounds: list[dict[str, Any]] = Field(default_factory=list)
    referenced_logs: list[Any] = Field(default_factory=list)
    process_steps: list[Any] = Field(default_factory=list)
    grant_feedback: bool = False


class RediagnoseRequest(_StrictModel):
    trigger_user_id: str = Field(min_length=1)
    hint: str | None = Field(default=None, max_length=2000)


class RediagnoseResponse(_StrictModel):
    session_id: str
    round_no: int
    round_id: str
    task_id: str


class AttachmentUploadResponse(_StrictModel):
    attachment_id: str
    path: str
    size_bytes: int
    sha256: str


class DashboardSummaryResponse(_StrictModel):
    generated_at: str
    product_id: str | None
    overview: dict[str, Any]
    by_product: list[dict[str, Any]]
    root_cause_distribution: dict[str, int]
    knowledge: dict[str, Any]
    runtime: dict[str, Any]
    confidence_histogram: dict[str, int]


_ROOT_CAUSE_CATEGORIES = Literal[
    "TEST_ENV_ERROR", "FW_DEFECT", "SCRIPT_ISSUE", "HW_DEFECT", "OTHER"
]


class FeedbackCorrection(_StrictModel):
    root_cause_category: _ROOT_CAUSE_CATEGORIES
    reason: str = Field(min_length=1)


class FeedbackRequest(_StrictModel):
    session_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    case_id: str = Field(default="")
    round_no: int | None = Field(default=None, gt=0)
    reviewer: str = Field(min_length=1)
    action: Literal["VALID", "INVALID"]
    correction: FeedbackCorrection | None = None


class FeedbackResponse(_StrictModel):
    feedback_id: str
    session_id: str
    round_no: int
    version: int
    status: Literal["applied", "idempotent"]
