from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from aitrace.persistence.models import UserQueue


def acquire_slot(session: Session, queue_key: str, now: datetime) -> bool:
    result = cast(
        CursorResult[Any],
        session.execute(
            update(UserQueue)
            .where(
                UserQueue.queue_key == queue_key,
                UserQueue.state == "ACTIVE",
                UserQueue.paused_until <= now,
                UserQueue.in_flight < 1,
            )
            .values(
                in_flight=UserQueue.in_flight + 1,
                state="ACTIVE",
                last_dispatch_at=now,
                updated_at=now,
            )
        ),
    )
    return result.rowcount == 1


def release_slot(session: Session, queue_key: str, now: datetime) -> None:
    session.execute(
        update(UserQueue)
        .where(UserQueue.queue_key == queue_key)
        .values(
            in_flight=func.greatest(UserQueue.in_flight - 1, 0),
            updated_at=now,
        )
    )
