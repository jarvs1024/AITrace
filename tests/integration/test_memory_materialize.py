# tests/integration/test_memory_materialize.py
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aitrace.ai.prompts import AnalysisContext, build_master_prompt
from aitrace.core.enums import QueueState, TaskType
from aitrace.knowledge.materialize import MemoryMaterializeWorker
from aitrace.knowledge.prescreen import prescreen_case_library
from aitrace.persistence.models import CaseLibrary, MemoryHotFile, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.models import DomainProfile
from aitrace.scheduling.types import NewTask

NOW = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)
DOMAIN = "domain1"

_PROFILE = DomainProfile(
    domain_id="domain1",
    product_ids=("ssd_9560",),
    log_sources=({"name": "test", "plugin": "test.txt"},),
    anchor_keywords={},
    analysis_focus="SSD 固件与硬件定界",
    skills_namespace="domain1_",
    memory_namespace="domain:domain1:",
    probe_whitelist=(),
)


def _seed_cl(
    db,
    *,
    fp: str,
    category: str = "FW_DEFECT",
    correction: str | None = None,
    conclusion: str = "固件状态机死锁",
    status: str = "lesson",
    use_hits: int = 0,
    observation_hits: int = 0,
    confirm_count: int = 1,
    updated_at: datetime = NOW,
) -> None:
    db.add(
        CaseLibrary(
            domain_id=DOMAIN,
            stable_fingerprint=fp,
            category=category,
            ai_conclusion=conclusion,
            correction=correction,
            evidence_summary="u0: assert device state LOCKED",
            exact_fingerprints=[f"exact-{fp}"],
            confirm_count=confirm_count,
            use_hits=use_hits,
            observation_hits=observation_hits,
            entry_status=status,
            version=1,
            updated_at=updated_at,
        )
    )
    db.flush()


_SEQ = 0


def _enqueue_materialize(db, domain_id: str = DOMAIN) -> None:
    global _SEQ
    _SEQ += 1
    queue = QueueRepository(db)
    queue.enqueue(
        NewTask(
            task_id=f"T-MEM-{domain_id}-{_SEQ}",
            batch_id=f"MEM-{domain_id}-{_SEQ}",
            session_id=None,
            round_id="",
            round_no=0,
            product_id="",
            case_id="",
            task_type=TaskType.MEMORY_MATERIALIZE,
            queue_key=domain_id,
            payload={"domain_id": domain_id},
        ),
        NOW,
    )


def test_materialize_writes_hot_file_ordered_excludes_demoted(db_session, tmp_path) -> None:
    _seed_cl(db_session, fp="fp-a", use_hits=10, updated_at=NOW)
    _seed_cl(db_session, fp="fp-b", use_hits=10, updated_at=NOW + timedelta(hours=1))
    _seed_cl(db_session, fp="fp-c", use_hits=99, status="demoted")
    _seed_cl(db_session, fp="fp-d", use_hits=5)
    _enqueue_materialize(db_session)

    worker = MemoryMaterializeWorker(QueueRepository(db_session))
    done = worker.run_once(db_session, data_dir=tmp_path, owner="w-1", now=NOW)

    assert done == 1
    hot = worker.hot_file_path(tmp_path, DOMAIN)
    assert hot.is_file()
    entries = json.loads(hot.read_text(encoding="utf-8"))
    assert [e["stable_fingerprint"] for e in entries] == ["fp-b", "fp-a", "fp-d"]
    assert all(e["stable_fingerprint"] != "fp-c" for e in entries)
    row = db_session.get(MemoryHotFile, DOMAIN)
    assert row is not None
    assert row.version == 1
    assert len(row.entries) == 3
    task = (
        db_session.execute(
            select(TaskQueue).where(TaskQueue.task_type == TaskType.MEMORY_MATERIALIZE)
        )
        .scalars()
        .one()
    )
    assert task.queue_state == QueueState.DONE.value


def test_materialize_upserts_version_and_file_on_rerun(db_session, tmp_path) -> None:
    _seed_cl(db_session, fp="fp-a", use_hits=1)
    _enqueue_materialize(db_session)
    worker = MemoryMaterializeWorker(QueueRepository(db_session))
    worker.run_once(db_session, data_dir=tmp_path, owner="w-1", now=NOW)

    _seed_cl(db_session, fp="fp-hot", use_hits=999)
    _enqueue_materialize(db_session, domain_id=DOMAIN)
    queue = QueueRepository(db_session)
    worker = MemoryMaterializeWorker(queue)
    done = worker.run_once(db_session, data_dir=tmp_path, owner="w-1", now=NOW)

    assert done == 1
    row = db_session.get(MemoryHotFile, DOMAIN)
    assert row.version == 2
    entries = json.loads(worker.hot_file_path(tmp_path, DOMAIN).read_text(encoding="utf-8"))
    assert entries[0]["stable_fingerprint"] == "fp-hot"


def test_prescreen_hit_counts_once_per_round_and_observing(db_session) -> None:
    _seed_cl(
        db_session,
        fp="fp-o",
        status="observing",
        use_hits=3,
        observation_hits=2,
        correction="初始化时序改为 T0+200ms",
    )

    hit1 = prescreen_case_library(
        db_session, domain_id=DOMAIN, stable_fingerprint="fp-o", round_id="r1", now=NOW
    )
    assert hit1 is not None
    assert hit1.observing is True
    assert hit1.hits == 4
    assert hit1.correction == "初始化时序改为 T0+200ms"

    hit2 = prescreen_case_library(
        db_session, domain_id=DOMAIN, stable_fingerprint="fp-o", round_id="r1", now=NOW
    )
    assert hit2 is not None and hit2.hits == 4

    hit3 = prescreen_case_library(
        db_session, domain_id=DOMAIN, stable_fingerprint="fp-o", round_id="r2", now=NOW
    )
    assert hit3 is not None and hit3.hits == 5

    row = (
        db_session.execute(
            select(CaseLibrary).where(
                CaseLibrary.domain_id == DOMAIN, CaseLibrary.stable_fingerprint == "fp-o"
            )
        )
        .scalars()
        .one()
    )
    assert row.use_hits == 5
    assert row.observation_hits == 4


def test_prescreen_post_observation_lesson_not_observing(db_session) -> None:
    _seed_cl(db_session, fp="fp-l", status="lesson", use_hits=9, observation_hits=9)
    hit = prescreen_case_library(
        db_session, domain_id=DOMAIN, stable_fingerprint="fp-l", round_id="r9", now=NOW
    )
    assert hit is not None
    assert hit.observing is False
    assert hit.hits == 10
    row = (
        db_session.execute(select(CaseLibrary).where(CaseLibrary.stable_fingerprint == "fp-l"))
        .scalars()
        .one()
    )
    assert row.observation_hits == 9


def test_prescreen_unknown_fingerprint_returns_none(db_session) -> None:
    hit = prescreen_case_library(
        db_session, domain_id=DOMAIN, stable_fingerprint="fp-missing", round_id="r1", now=NOW
    )
    assert hit is None


def test_master_prompt_includes_prescreen_hit_and_observing_marker() -> None:
    ctx = AnalysisContext(
        authoritative={"product_id": "ssd_9560"},
        summary={"status": "CLEANED"},
        slices={"baseline": "line"},
        profile=_PROFILE,
        memory_hits=(
            {
                "stable_fingerprint": "fp-o",
                "category": "FW_DEFECT",
                "correction": "初始化时序改为 T0+200ms",
                "ai_conclusion": "旧结论",
                "observing": True,
            },
        ),
    )
    prompt = build_master_prompt(ctx)
    assert "初始化时序改为 T0+200ms" in prompt
    assert "观察期" in prompt
