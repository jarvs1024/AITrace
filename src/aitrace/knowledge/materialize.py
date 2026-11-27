# src/aitrace/knowledge/materialize.py
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.models import CaseLibrary, MemoryHotFile
from aitrace.persistence.repositories.queue import QueueRepository


def _namespace_stem(domain_id: str) -> str:
    # 与画像 memory_namespace（domain:{domain_id}:）同源派生热文件路径
    return f"domain:{domain_id}:".rstrip(":").replace(":", "_")


class MemoryMaterializeWorker:
    def __init__(self, queue: QueueRepository, *, top_n: int = 200) -> None:
        self._queue = queue
        self._top_n = top_n

    def hot_file_path(self, data_dir: Path, domain_id: str) -> Path:
        return data_dir / "memory" / f"{_namespace_stem(domain_id)}.json"

    def refresh_domain(
        self, session: Session, domain_id: str, *, data_dir: Path, now: datetime
    ) -> str:
        rows = session.execute(
            select(CaseLibrary)
            .where(
                CaseLibrary.domain_id == domain_id,
                CaseLibrary.entry_status != "demoted",
            )
            .order_by(CaseLibrary.use_hits.desc(), CaseLibrary.updated_at.desc())
            .limit(self._top_n)
        ).scalars()
        entries = [
            {
                "stable_fingerprint": row.stable_fingerprint,
                "category": row.category,
                "ai_conclusion": row.ai_conclusion,
                "correction": row.correction,
                "confirm_count": row.confirm_count,
                "use_hits": row.use_hits,
                "observation_hits": row.observation_hits,
                "entry_status": row.entry_status,
                "updated_at": row.updated_at.isoformat(),
            }
            for row in rows
        ]
        path = self.hot_file_path(data_dir, domain_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        existing = session.get(MemoryHotFile, domain_id)
        if existing is None:
            session.add(
                MemoryHotFile(domain_id=domain_id, entries=entries, version=1, generated_at=now)
            )
        else:
            existing.entries = entries
            existing.version += 1
            existing.generated_at = now
        session.flush()
        return str(path)

    def run_once(self, session: Session, *, data_dir: Path, owner: str, now: datetime) -> int:
        task = self._queue.claim_generic(owner, now, TaskType.MEMORY_MATERIALIZE)
        if task is None:
            return 0
        domain_id = (task.payload or {}).get("domain_id") or task.queue_key
        self.refresh_domain(session, domain_id, data_dir=data_dir, now=now)
        self._queue.finish(
            task.task_id, task.lease_owner or "", task.lease_generation, QueueState.DONE, now
        )
        return 1
