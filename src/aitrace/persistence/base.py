from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Base(DeclarativeBase):
    pass
