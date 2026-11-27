from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.persistence.models import Profile
from aitrace.profiles.resolver import ResolvedProfile


class ProfileSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, resolved: ResolvedProfile, now: datetime) -> None:
        self._session.execute(
            insert(Profile)
            .values(
                domain_id=resolved.domain_id,
                config=resolved.config.model_dump(mode="json"),
                version=resolved.version,
                updated_by="system",
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["domain_id"],
                set_={
                    "config": resolved.config.model_dump(mode="json"),
                    "version": resolved.version,
                    "updated_by": "system",
                    "updated_at": now,
                },
            )
        )
