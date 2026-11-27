from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from aitrace.probe.whitelist import GLOBAL_PROBE_WHITELIST


class SettingsError(RuntimeError):
    """Raised when configuration is missing or invalid; the service must not start."""


_FIELD_LABELS = {
    "env": "运行环境",
    "server": "服务端配置",
    "database": "数据库配置",
    "queue": "队列配置",
    "storage": "存储配置",
    "service_token": "服务令牌",
    "service_token_file": "服务令牌文件",
    "server.host": "监听地址",
    "server.port": "服务端口",
    "server.log_level": "日志级别",
    "database.dsn": "数据库连接串",
    "database.pool_size": "数据库连接池大小",
    "database.statement_timeout_ms": "数据库语句超时",
    "queue.cleaning_concurrency": "清洗池并发",
    "queue.ai_worker_concurrency": "AI Worker 并发",
    "queue.per_token_depth": "单令牌队列深度",
    "queue.global_depth": "全局队列深度",
    "queue.lease_seconds": "任务租约时长",
    "queue.ai_hard_timeout_seconds": "AI 硬超时",
    "queue.aging_interval_seconds": "优先级老化间隔",
    "storage.data_dir": "数据根目录",
    "storage.max_attachment_bytes": "附件大小上限",
    "storage.profile_domains_dir": "领域画像目录",
    "storage.l2_quota_bytes": "L2 原始日志容量配额",
    "storage.l1_ttl_days": "证据保留天数（L1 TTL）",
    "storage.l2_ttl_days": "原始日志保留天数（L2 TTL）",
    "storage.watermark_warn_fraction": "磁盘水位预警比例",
    "storage.watermark_force_fraction": "磁盘水位强制清理比例",
    "ai": "AI 配置",
    "ai.edition": "AI 版本",
    "ai.model": "AI 模型",
    "ai.vpc_endpoint": "AI VPC 网关端点",
    "ai.sdk_control_timeout_ms": "AI SDK 控制请求超时",
    "ai.max_turns": "AI 最大轮数",
    "ai.query_timeout_seconds": "AI 查询超时",
    "ai.token_request_timeout_seconds": "AI 令牌请求超时",
    "ai.pause_backoff_base_seconds": "暂停退避基数",
    "ai.pause_backoff_max_seconds": "暂停退避上限",
    "ai.pause_abandon_seconds": "暂停放弃时长",
    "ai.waiting_timeout_seconds": "AI 等待超时",
    "ai.batch_ai_budget": "批处理 AI 预算",
    "ai.low_confidence_threshold": "低置信度阈值",
    "ai.escalation_min_group": "升级最小分组数",
    "ai.escalation_majority": "升级多数表决",
    "ai.fake_scenario_file": "Fake 场景文件",
    "aitest": "aitest 配置",
    "aitest.base_url": "aitest 服务地址",
    "aitest.read_token": "aitest 读取令牌",
    "aitest.read_token_file": "aitest 读取令牌文件",
    "aitest.timeout_seconds": "aitest 请求超时",
    "runtime": "运行时调度配置",
    "runtime.enabled": "运行时调度开关",
    "runtime.tick_seconds": "运行时调度周期",
    "probe": "SSH 探针配置",
    "probe.global_whitelist": "探针全局白名单",
    "probe.connect_timeout_seconds": "探针连接超时",
    "probe.command_timeout_seconds": "探针命令超时",
    "slo": "SLO 标定参数",
    "slo.ai_p95_baseline_seconds": "AI 任务 P95 基线（D）",
    "slo.queue_to_clean_p95_seconds": "排队转清洗 P95 目标",
    "slo.e2e_first_case_p95_seconds": "首例端到端 P95 目标",
    "slo.e2e_queued_case_p95_seconds": "有前序积压用例端到端 P95 目标",
    "slo.e2e_batch_p95_seconds": "批次全任务 P95 目标",
    "slo.fairness_max_gap_seconds": "跨用户公平性最大等待差",
    "slo.probe_max_age_minutes": "探针时效上限",
    "monitor": "监控配置",
    "monitor.enabled": "监控巡检开关",
    "monitor.check_interval_seconds": "巡检周期",
    "monitor.global_depth_warn_fraction": "全局队列深度告警比例",
    "monitor.per_token_depth_warn": "单令牌队列深度告警值",
    "monitor.paused_auth_warn_seconds": "PAUSED_AUTH 持续告警时长",
    "monitor.token_fetch_fail_rate": "令牌拉取失败率告警阈值",
    "monitor.callback_fail_rate": "回调失败率告警阈值",
    "monitor.callback_backlog_seconds": "回调待发送积压告警时长",
    "monitor.fencing_reject_warn_seconds": "fencing 拒绝持续告警时长",
    "monitor.worker_util_low": "Worker 利用率下限",
    "monitor.worker_util_high": "Worker 利用率上限",
    "monitor.ops_webhook": "运维告警群 webhook",
    "backup": "备份配置",
    "backup.staging_dir": "备份暂存目录",
    "backup.keep_count": "备份保留数",
    "backup.remote_dest": "异盘/异地推送目标",
    "backup.min_free_bytes": "备份前最小可用空间",
    "release_root": "发布根目录",
}


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dsn: str = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace"
    pool_size: int = Field(default=10, ge=1, le=100)
    statement_timeout_ms: int = Field(default=30_000, ge=1_000)


class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleaning_concurrency: int = Field(default=4, ge=1, le=64)
    ai_worker_concurrency: int = Field(default=4, ge=1, le=32)
    per_token_depth: int = Field(default=100, ge=1)
    global_depth: int = Field(default=500, ge=1)
    lease_seconds: int = Field(default=600, ge=30)
    ai_hard_timeout_seconds: int = Field(default=1800, ge=60)
    aging_interval_seconds: int = Field(default=300, ge=30)


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    max_attachment_bytes: int = Field(default=50 * 1024 * 1024, ge=1024 * 1024)
    profile_domains_dir: Path = Path("deploy/config/domains")
    cutoff_rules_file: Path = Path("deploy/config/rules/cutoff.yaml")
    l2_quota_bytes: int | None = Field(default=None, ge=1)
    l1_ttl_days: int = Field(default=30, ge=1)
    l2_ttl_days: int = Field(default=30, ge=1)
    watermark_warn_fraction: float = Field(default=0.70, gt=0, le=1)
    watermark_force_fraction: float = Field(default=0.85, gt=0, le=1)


class AISettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    edition: Literal["cn", "intl", "fake"] = "cn"
    model: str = "GLM-5.3-Flash"
    vpc_endpoint: str | None = None
    sdk_control_timeout_ms: int = Field(default=180_000, ge=10_000)
    max_turns: int = Field(default=40, ge=1, le=200)
    query_timeout_seconds: int = Field(default=1200, ge=60)
    token_request_timeout_seconds: float = Field(default=10.0, ge=1.0)
    pause_backoff_base_seconds: int = Field(default=60, ge=1)
    pause_backoff_max_seconds: int = Field(default=1800, ge=60)
    pause_abandon_seconds: int = Field(default=21600, ge=600)
    waiting_timeout_seconds: int = Field(default=1800, ge=60)
    batch_ai_budget: int = Field(default=3, ge=1, le=20)
    low_confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    escalation_min_group: int = Field(default=5, ge=2)
    escalation_majority: bool = True
    fake_scenario_file: Path | None = None


class AitestSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str = "http://127.0.0.1:8090"
    read_token: SecretStr | None = None
    read_token_file: Path | None = None
    timeout_seconds: float = Field(default=10.0, ge=0.5)


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    tick_seconds: float = Field(default=1.0, ge=0.1, le=60)


class ProbeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    global_whitelist: list[str] = Field(default_factory=lambda: list(GLOBAL_PROBE_WHITELIST))
    host_allowlist: list[str] = Field(default_factory=list)
    allow_loopback: bool = Field(default=True)
    connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    command_timeout_seconds: int = Field(default=10, ge=1, le=300)

    @field_validator("host_allowlist")
    @classmethod
    def _valid_asset_entries(cls, value: list[str]) -> list[str]:
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"probe.host_allowlist 条目非法：{entry!r}"
                    f"（须为 IP 或 CIDR，如 10.0.0.0/24）：{exc}"
                ) from exc
        return value


class SloSettings(BaseModel):
    """spec §7.1 阶段二 SLO 初始参考值；M4 标定后回填收敛值。"""

    model_config = ConfigDict(extra="forbid")
    ai_p95_baseline_seconds: int = Field(default=360, ge=60)
    queue_to_clean_p95_seconds: int = Field(default=120, ge=10)
    e2e_first_case_p95_seconds: int = Field(default=900, ge=60)
    e2e_queued_case_p95_seconds: int = Field(default=2700, ge=60)
    e2e_batch_p95_seconds: int = Field(default=3600, ge=60)
    fairness_max_gap_seconds: int = Field(default=720, ge=60)
    probe_max_age_minutes: int = Field(default=30, ge=1)


class MonitorSettings(BaseModel):
    """spec §7.3 告警阈值（本地可判定项）。"""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    check_interval_seconds: float = Field(default=60.0, ge=5.0, le=3600)
    global_depth_warn_fraction: float = Field(default=0.8, gt=0, le=1)
    per_token_depth_warn: int = Field(default=100, ge=1)
    paused_auth_warn_seconds: int = Field(default=600, ge=60)
    token_fetch_fail_rate: float = Field(default=0.10, gt=0, le=1)
    callback_fail_rate: float = Field(default=0.05, gt=0, le=1)
    callback_backlog_seconds: int = Field(default=600, ge=60)
    fencing_reject_warn_seconds: int = Field(default=600, ge=60)
    worker_util_low: float = Field(default=0.50, ge=0, le=1)
    worker_util_high: float = Field(default=0.90, ge=0, le=1)
    ops_webhook: str | None = None


class BackupSettings(BaseModel):
    """spec §8.6 备份策略。"""

    model_config = ConfigDict(extra="forbid")
    staging_dir: Path = Path("data/backups")
    keep_count: int = Field(default=3, ge=1)
    remote_dest: str | None = None
    min_free_bytes: int = Field(default=4 * 1024**3, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AITRACE_",
        env_nested_delimiter="__",
        extra="forbid",
        env_file=None,
    )

    env: Literal["dev", "staging", "prod"] = "dev"
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    service_token: SecretStr | None = None
    service_token_file: Path | None = None
    ai: AISettings = Field(default_factory=AISettings)
    aitest: AitestSettings = Field(default_factory=AitestSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    probe: ProbeSettings = Field(default_factory=ProbeSettings)
    slo: SloSettings = Field(default_factory=SloSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    release_root: Path | None = None

    @model_validator(mode="after")
    def _resolve_secret_files(self) -> Self:
        if self.service_token is None and self.service_token_file is not None:
            self.service_token = SecretStr(
                _read_secret_file(self.service_token_file, "服务令牌文件（service_token_file）")
            )
        if self.aitest.read_token is None and self.aitest.read_token_file is not None:
            self.aitest.read_token = SecretStr(
                _read_secret_file(
                    self.aitest.read_token_file,
                    "aitest 读取令牌文件（aitest.read_token_file）",
                )
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Environment variables outrank the YAML base layer."""
        return (env_settings, init_settings)


def load_settings(config_path: Path | None = None) -> Settings:
    values: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise SettingsError(f"配置文件不存在：{config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            raise SettingsError(f"配置文件顶层必须是映射：{config_path}")
        values = raw or {}
    try:
        settings = Settings(**values)
    except ValidationError as exc:
        raise SettingsError(_format_validation_error(exc)) from exc

    if settings.service_token is None:
        raise SettingsError(
            "配置校验失败，服务拒绝启动：\n"
            "- 服务令牌（service_token）：未提供；要求：设置 AITRACE_SERVICE_TOKEN，"
            "或通过 AITRACE_SERVICE_TOKEN_FILE 指向 LoadCredential 文件"
        )
    return settings


_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "server": ServerSettings,
    "database": DatabaseSettings,
    "queue": QueueSettings,
    "storage": StorageSettings,
    "ai": AISettings,
    "aitest": AitestSettings,
    "runtime": RuntimeSettings,
    "probe": ProbeSettings,
    "slo": SloSettings,
    "monitor": MonitorSettings,
    "backup": BackupSettings,
}


def _describe_read_failure(exc: OSError | UnicodeDecodeError) -> str:
    """Describe a failed token-file read without echoing any file contents."""
    if isinstance(exc, UnicodeDecodeError):
        return "内容不是合法的 UTF-8 文本"
    if isinstance(exc, FileNotFoundError):
        return "文件不存在"
    if isinstance(exc, PermissionError):
        return "没有读取权限"
    if isinstance(exc, IsADirectoryError):
        return "路径是目录而非文件"
    return exc.strerror or type(exc).__name__


def _read_secret_file(path: Path, label: str) -> str:
    """Read a secret file for the field `label`; Chinese error, never echoes contents."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SettingsError(
            "配置校验失败，服务拒绝启动：\n"
            f"- {label}：无法读取 {path}；原因：{_describe_read_failure(exc)}；"
            "要求：确保该路径存在、可读且为 UTF-8 文本的 LoadCredential 文件"
        ) from exc
    if not token:
        raise SettingsError(
            f"配置校验失败，服务拒绝启动：\n- {label} 为空；要求：文件内容需为非空白文本"
        )
    return token


def _allowed_child_keys(parent: str) -> tuple[str, ...]:
    """Keys allowed under a section (empty parent means the top level)."""
    model: type[BaseModel] | None = Settings if not parent else _SECTION_MODELS.get(parent)
    if model is None:
        return ()
    return tuple(model.model_fields)


def _requirement(error: Mapping[str, Any], path: str) -> str:
    """Chinese requirement clause; falls back to pydantic's text for constraints."""
    kind = str(error["type"])
    parent = path.rpartition(".")[0]
    if kind == "extra_forbidden":
        allowed = _allowed_child_keys(parent)
        if not allowed:
            return "不支持的配置项"
        scope = f"{parent} 下" if parent else "顶层"
        return f"不支持的配置项；{scope}允许的配置项为：" + "、".join(allowed)
    if kind == "model_type":
        allowed = _allowed_child_keys(path)
        if not allowed:
            return "必须是映射（YAML 键值对）"
        return "必须是映射（YAML 键值对），可包含子项：" + "、".join(allowed)
    return str(error["msg"])


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["配置校验失败，服务拒绝启动："]
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"])
        label = _FIELD_LABELS.get(path, path)
        supplied = error.get("input")
        current = "<缺失>" if supplied is None else repr(supplied)
        lines.append(f"- {label}（{path}）：当前值 {current}；要求：{_requirement(error, path)}")
    return "\n".join(lines)
