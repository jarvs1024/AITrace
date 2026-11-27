import sys

import uvicorn

from aitrace.api.app import create_app
from aitrace.observability.logging import configure_logging
from aitrace.ops.startup import StartupError, run_startup_checks


def run() -> None:
    app = create_app()
    settings = app.state.settings
    configure_logging(settings.server.log_level)
    try:
        run_startup_checks(settings, app.state.session_factory)
    except StartupError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
