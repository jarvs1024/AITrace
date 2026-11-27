from types import SimpleNamespace
from unittest.mock import patch

import pytest

import aitrace.main as main


def test_run_starts_uvicorn_with_app_and_settings() -> None:
    settings = SimpleNamespace(
        server=SimpleNamespace(host="127.0.0.1", port=8080, log_level="INFO")
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=object()))
    with (
        patch.object(main, "create_app", return_value=app),
        patch.object(main, "run_startup_checks") as startup_checks,
        patch.object(main.uvicorn, "run") as uvicorn_run,
    ):
        main.run()
    startup_checks.assert_called_once_with(settings, app.state.session_factory)
    uvicorn_run.assert_called_once_with(app, host="127.0.0.1", port=8080)


def test_run_exits_when_startup_checks_fail(capsys) -> None:
    settings = SimpleNamespace(
        server=SimpleNamespace(host="127.0.0.1", port=8080, log_level="INFO")
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, session_factory=object()))
    with (
        patch.object(main, "create_app", return_value=app),
        patch.object(main, "run_startup_checks", side_effect=main.StartupError("自检失败")),
        patch.object(main.uvicorn, "run") as uvicorn_run,
        pytest.raises(SystemExit) as exc_info,
    ):
        main.run()
    assert exc_info.value.code == 1
    uvicorn_run.assert_not_called()
    assert "自检失败" in capsys.readouterr().err
