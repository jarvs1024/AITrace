from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.api.schemas import DeliveryPayload, DeliveryResponse, SessionResult
from aitrace.core.enums import Stage, TaskType
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.persistence.models import Batch, Round, SessionModel
from aitrace.persistence.repositories.profiles import ProfileSnapshotRepository
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.resolver import ProfileResolver
from aitrace.scheduling.types import NewTask


@dataclass(frozen=True)
class IngestOutcome:
    response: DeliveryResponse
    created_any: bool


class IngestService:
    def __init__(
        self,
        session: Session,
        resolver: ProfileResolver,
        clock: SystemClock,
        ids: UuidIdGenerator,
    ) -> None:
        self._session = session
        self._resolver = resolver
        self._clock = clock
        self._ids = ids

    def deliver(
        self, payload: DeliveryPayload, *, pre_checks: list[Callable[[], None]] | None = None
    ) -> IngestOutcome:
        for check in pre_checks or []:
            check()
        now = self._clock.now()
        profile = self._resolver.resolve(payload.product_id)
        batch_task_id = self._upsert_batch(payload, now)
        ProfileSnapshotRepository(self._session).upsert(profile, now)
        queue = QueueRepository(self._session)

        results: list[SessionResult] = []
        created_any = False
        for case in payload.failed_cases:
            session_id = self._ids.new("S")
            inserted = self._session.execute(
                insert(SessionModel)
                .values(
                    session_id=session_id,
                    product_id=payload.product_id,
                    batch_id=payload.batch_id,
                    case_id=case.case_id,
                    domain_id=profile.domain_id,
                    profile_version=profile.version,
                    trigger_user_id=payload.trigger_user_id,
                    hardware_type=payload.hardware_type,
                    stable_fingerprint=None,
                    env_topology=payload.env_topology.model_dump(mode="json"),
                    git_context=payload.git_context.model_dump(mode="json"),
                    latest_round_no=1,
                    status="RUNNING",
                    citation_group_id=None,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["product_id", "batch_id", "case_id"])
                .returning(SessionModel.session_id)
            ).scalar_one_or_none()

            if inserted is None:
                existing = self._session.execute(
                    select(SessionModel.session_id).where(
                        SessionModel.product_id == payload.product_id,
                        SessionModel.batch_id == payload.batch_id,
                        SessionModel.case_id == case.case_id,
                    )
                ).scalar_one()
                results.append(
                    SessionResult(
                        case_id=case.case_id,
                        session_id=existing,
                        is_existing=True,
                        status="EXISTING",
                    )
                )
                continue

            created_any = True
            round_id = self._ids.new("R")
            self._session.add(
                Round(
                    round_id=round_id,
                    session_id=session_id,
                    round_no=1,
                    trigger_user_id=payload.trigger_user_id,
                    queue_key=payload.trigger_user_id,
                    exact_fingerprint=None,
                    stage=Stage.QUEUED,
                    round_status="RUNNING",
                    report_id=None,
                    confidence=None,
                    feedback=None,
                    started_at=now,
                    finished_at=None,
                )
            )
            queue.enqueue(
                NewTask(
                    task_id=self._ids.new("T"),
                    batch_id=payload.batch_id,
                    session_id=session_id,
                    round_id=round_id,
                    round_no=1,
                    product_id=payload.product_id,
                    case_id=case.case_id,
                    task_type=TaskType.CASE_CLEAN,
                    queue_key=payload.trigger_user_id,
                    payload={
                        "script_path": case.script_path,
                        "failed_at_utc": case.failed_at_utc,
                        "env_binding": case.env_binding,
                        "logs": case.logs.model_dump(mode="json"),
                    },
                ),
                now,
            )
            results.append(
                SessionResult(
                    case_id=case.case_id,
                    session_id=session_id,
                    is_existing=False,
                    status="ACCEPTED",
                )
            )

        self._session.flush()
        return IngestOutcome(
            response=DeliveryResponse(
                accepted=True,
                batch_task_id=batch_task_id,
                sessions=results,
                rejected=[],
            ),
            created_any=created_any,
        )

    def _upsert_batch(self, payload: DeliveryPayload, now: datetime) -> str:
        suffix = payload.batch_id.removeprefix("BATCH-")
        batch_task_id = f"T-BATCH-{suffix}"
        failed = [case.model_dump(mode="json") for case in payload.failed_cases]
        passed = [case.model_dump(mode="json") for case in payload.passed_cases]
        self._session.execute(
            insert(Batch)
            .values(
                batch_id=payload.batch_id,
                product_id=payload.product_id,
                trigger_user_id=payload.trigger_user_id,
                batch_task_id=batch_task_id,
                failed_cases=failed,
                passed_cases=passed,
                batch_state="RUNNING",
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["batch_id"],
                set_={
                    "failed_cases": failed,
                    "passed_cases": passed,
                    "trigger_user_id": payload.trigger_user_id,
                    "updated_at": now,
                },
            )
        )
        return batch_task_id
