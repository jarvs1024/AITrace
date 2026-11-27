import os

from aitrace.core.settings import RuntimeSettings, load_settings


def test_runtime_defaults_enabled() -> None:
    settings = RuntimeSettings()
    assert settings.enabled is True
    assert settings.tick_seconds == 1.0


def test_runtime_enabled_env_flag() -> None:
    os.environ["AITRACE_SERVICE_TOKEN"] = "test-service-token"
    os.environ["AITRACE_RUNTIME__ENABLED"] = "false"
    try:
        settings = load_settings()
        assert settings.runtime.enabled is False
    finally:
        os.environ.pop("AITRACE_RUNTIME__ENABLED", None)
        os.environ.pop("AITRACE_SERVICE_TOKEN", None)
