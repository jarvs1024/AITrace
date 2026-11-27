from enum import StrEnum


class TaskType(StrEnum):
    CASE_CLEAN = "case_clean"
    CASE_AI = "case_ai"
    BATCH_COMMON = "batch_common"
    NOTIFY = "notify"
    CALLBACK = "callback"
    MEMORY_MATERIALIZE = "memory_materialize"


class QueueState(StrEnum):
    PENDING = "PENDING"
    CLEANING = "CLEANING"
    WAITING = "WAITING"
    READY = "READY"
    AI_RUNNING = "AI_RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class DispatchPhase(StrEnum):
    JIT_FETCH = "JIT_FETCH"
    AI_EXEC = "AI_EXEC"


class Stage(StrEnum):
    QUEUED = "QUEUED"
    CLEANING = "CLEANING"
    AI_CLASSIFYING = "AI_CLASSIFYING"
    DEEP_PROBING = "DEEP_PROBING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WaitingReason(StrEnum):
    TOKEN_BUSY = "token_busy"
    CITATION_PENDING = "citation_pending"
    NOT_BEFORE = "not_before"
    RETRY_BACKOFF = "retry_backoff"
    PAUSED_AUTH = "paused_auth"


class UserQueueState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED_AUTH = "PAUSED_AUTH"


class BatchState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
