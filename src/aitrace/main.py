import uvicorn

from aitrace.api.app import create_app


def run() -> None:
    app = create_app()
    settings = app.state.settings
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
