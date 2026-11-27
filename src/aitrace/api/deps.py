from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings
from aitrace.profiles.resolver import ProfileResolver

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory.begin() as session:
        yield session


def get_resolver(request: Request) -> ProfileResolver:
    return request.app.state.profile_resolver


def get_clock(request: Request) -> SystemClock:
    return request.app.state.clock


def get_ids(request: Request) -> UuidIdGenerator:
    return request.app.state.ids


def require_service_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AitraceError("FORBIDDEN", "服务令牌缺失或格式非法", http_status=403)
    expected = settings.service_token.get_secret_value() if settings.service_token else ""
    if not expected or credentials.credentials != expected:
        raise AitraceError("FORBIDDEN", "服务令牌无效", http_status=403)
