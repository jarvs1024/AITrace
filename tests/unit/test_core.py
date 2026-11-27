from datetime import UTC, datetime

from aitrace.core.enums import (
    BatchState,
    DispatchPhase,
    QueueState,
    Stage,
    TaskType,
    UserQueueState,
    WaitingReason,
)
from aitrace.core.ids import SystemClock, UuidIdGenerator


def test_enum_values_match_contract() -> None:
    assert TaskType.MEMORY_MATERIALIZE == "memory_materialize"
    assert QueueState.AI_RUNNING == "AI_RUNNING"
    assert DispatchPhase.AI_EXEC == "AI_EXEC"
    assert Stage.DEEP_PROBING == "DEEP_PROBING"
    assert WaitingReason.RETRY_BACKOFF == "retry_backoff"
    assert UserQueueState.PAUSED_AUTH == "PAUSED_AUTH"
    assert BatchState.COMPLETED == "COMPLETED"


def test_system_clock_returns_utc_time() -> None:
    assert SystemClock().now().tzinfo is UTC


def test_uuid_id_generator_builds_prefixed_dated_ids() -> None:
    generator = UuidIdGenerator()
    first = generator.new("case")
    second = generator.new("case")
    assert first.startswith("case-")
    assert first.split("-")[1] == datetime.now(UTC).strftime("%Y%m%d")
    assert first != second
