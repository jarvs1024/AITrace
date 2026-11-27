from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aitrace.core.enums import TaskType


@dataclass(frozen=True)
class NewTask:
    task_id: str
    batch_id: str
    session_id: str | None
    round_id: str
    round_no: int
    product_id: str
    case_id: str
    task_type: TaskType
    queue_key: str
    priority: int = 1
    max_attempts: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
