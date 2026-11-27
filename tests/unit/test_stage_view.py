from datetime import UTC, datetime, timedelta

import pytest

from aitrace.scheduling.stage_view import build_stage_view

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class Task:
    def __init__(self, **kwargs: object) -> None:
        self.queue_state = "PENDING"
        self.dispatch_phase = None
        self.next_retry_at = None
        self.depends_on_task_id = None
        self.not_before = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class Queue:
    def __init__(self, state: str = "ACTIVE") -> None:
        self.state = state


@pytest.mark.parametrize(
    ("task", "queue", "stage", "progress", "reason"),
    [
        (Task(queue_state="PENDING"), Queue(), "QUEUED", 10, None),
        (Task(queue_state="CLEANING"), Queue(), "CLEANING", 30, None),
        (Task(queue_state="READY"), Queue(), "QUEUED", 10, "token_busy"),
        (Task(queue_state="READY"), Queue("PAUSED_AUTH"), "QUEUED", 10, "paused_auth"),
        (
            Task(queue_state="AI_RUNNING", dispatch_phase="JIT_FETCH"),
            Queue(),
            "QUEUED",
            10,
            "token_busy",
        ),
        (
            Task(queue_state="AI_RUNNING", dispatch_phase="AI_EXEC"),
            Queue(),
            "AI_CLASSIFYING",
            50,
            None,
        ),
        (Task(queue_state="DONE"), Queue(), "COMPLETED", 100, None),
        (Task(queue_state="FAILED"), Queue(), "FAILED", None, None),
    ],
)
def test_stage_mapping(
    task: Task, queue: Queue, stage: str, progress: int | None, reason: str | None
) -> None:
    view = build_stage_view(task, queue, NOW)  # type: ignore[arg-type]
    assert view.stage == stage
    assert view.progress == progress
    assert view.waiting_reason == reason


def test_retry_backoff_takes_precedence_for_pending() -> None:
    task = Task(queue_state="PENDING", next_retry_at=NOW + timedelta(seconds=30))
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.waiting_reason == "retry_backoff"


def test_waiting_dependency_maps_to_citation_pending() -> None:
    task = Task(queue_state="WAITING", depends_on_task_id="T0")
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.stage == "QUEUED"
    assert view.waiting_reason == "citation_pending"


def test_not_before_maps_to_not_before() -> None:
    task = Task(queue_state="WAITING", not_before=NOW + timedelta(minutes=5))
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.waiting_reason == "not_before"
