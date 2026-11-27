# src/aitrace/profiles/models.py
from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_ROOT_CAUSES = ("TEST_ENV_ERROR", "FW_DEFECT", "SCRIPT_ISSUE", "HW_DEFECT", "OTHER")
_FORBIDDEN_PROBE = re.compile(
    r"(?i)\b(format|fw-download|fw-commit|reset|delete|write|sanitize|set-feature)\b"
)


class LogSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["test", "uart", "dmesg", "scheduler"]
    plugin: str = Field(min_length=1)


class SliceParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_lines: int = Field(default=200, ge=0)
    first_error_before_s: int = Field(default=60, ge=0)
    first_error_after_s: int = Field(default=15, ge=0)
    tail_lines: int = Field(default=100, ge=0)
    small_log_full_threshold_bytes: int = Field(default=1024 * 1024, ge=0)
    first_error_window_budget_bytes: int = Field(default=5 * 1024 * 1024, ge=0)


class ConfidenceWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    log_completeness: float = Field(default=0.15, ge=0, le=1)
    time_alignment: float = Field(default=0.15, ge=0, le=1)
    error_info_completeness: float = Field(default=0.10, ge=0, le=1)
    process_error_rate: float = Field(default=0.10, ge=0, le=1)
    evidence_chain: float = Field(default=0.15, ge=0, le=1)
    reference_hit: float = Field(default=0.20, ge=0, le=1)
    consistency_check: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"置信度权重之和必须为 1.0，当前为 {total:.3f}")
        return self


class DomainProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    product_ids: tuple[str, ...] = ()
    log_sources: tuple[LogSource, ...]
    cleaning_rules: dict[str, Any] = Field(default_factory=dict)
    anchor_keywords: dict[str, tuple[str, ...]]
    slice_params: SliceParams = Field(default_factory=SliceParams)
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    analysis_focus: str = Field(min_length=1)
    prompt_template: str = ""
    skills_namespace: str
    memory_namespace: str
    root_cause_enum: tuple[str, ...] = DEFAULT_ROOT_CAUSES
    probe_whitelist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        names = [source.name for source in self.log_sources]
        if len(names) != len(set(names)):
            raise ValueError("日志源重复：log_sources 中同一 name 只能出现一次")
        if not set(DEFAULT_ROOT_CAUSES).issubset(self.root_cause_enum):
            raise ValueError(f"根因闭集必须包含默认五类：{DEFAULT_ROOT_CAUSES}")
        if self.skills_namespace != f"{self.domain_id}_":
            raise ValueError(f"skills_namespace 必须为 {self.domain_id}_")
        if self.memory_namespace != f"domain:{self.domain_id}:":
            raise ValueError(f"memory_namespace 必须为 domain:{self.domain_id}:")
        for command in self.probe_whitelist:
            if _FORBIDDEN_PROBE.search(command):
                raise ValueError(f"探针白名单只允许只读命令，非法命令：{command}")
        return self
