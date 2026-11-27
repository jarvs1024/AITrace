import uvicorn

from aitrace.api.app import create_app
from aitrace.observability.logging import configure_logging


def run() -> None:
    app = create_app()
    settings = app.state.settings
    configure_logging(settings.server.log_level)
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
