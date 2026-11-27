from types import SimpleNamespace
from unittest.mock import patch

import aitrace.main as main


def test_run_starts_uvicorn_with_app_and_settings() -> None:
    settings = SimpleNamespace(
        server=SimpleNamespace(host="127.0.0.1", port=8080, log_level="INFO")
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    with (
        patch.object(main, "create_app", return_value=app),
        patch.object(main.uvicorn, "run") as uvicorn_run,
    ):
        main.run()
    uvicorn_run.assert_called_once_with(app, host="127.0.0.1", port=8080)
