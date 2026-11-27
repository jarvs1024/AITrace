from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from aitrace import __version__
from aitrace.api.deps import get_settings
from aitrace.core.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    return {"status": "ok", "version": __version__, "env": settings.env}


@router.get("/readyz", response_model=None)
def readyz(request: Request) -> JSONResponse | dict[str, str]:
    if request.app.state.stopping:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "stopping"})
    if not request.app.state.ready:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "starting"})
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503, content={"status": "not_ready", "reason": "db_unavailable"}
        )
    return {"status": "ready", "version": __version__}
