from typing import Annotated

from fastapi import APIRouter, Depends

from aitrace import __version__
from aitrace.api.deps import get_settings
from aitrace.core.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> dict[str, str]:
    return {"status": "ok", "version": __version__, "env": settings.env}
