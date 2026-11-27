# tests/unit/test_settings_ai.py
from aitrace.core.settings import AISettings, load_settings


def test_ai_defaults_pin_glm_5_3_flash() -> None:
    ai = AISettings()
    assert ai.model == "GLM-5.3-Flash"
    assert ai.edition == "cn"
    assert ai.batch_ai_budget == 3
    assert ai.pause_abandon_seconds == 21600
    assert ai.low_confidence_threshold == 0.5


def test_ai_settings_reject_bad_edition() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AISettings(edition="us")


def test_load_settings_env_override_ai_model(tmp_path) -> None:
    import os

    os.environ["AITRACE_AI__MODEL"] = "glm-5.3-flash-x"
    os.environ["AITRACE_SERVICE_TOKEN"] = "test-service-token"
    try:
        settings = load_settings()
        assert settings.ai.model == "glm-5.3-flash-x"
    finally:
        os.environ.pop("AITRACE_AI__MODEL", None)
        os.environ.pop("AITRACE_SERVICE_TOKEN", None)


def test_aitest_read_token_file_loaded(tmp_path) -> None:
    import yaml

    token_file = tmp_path / "aitest.token"
    token_file.write_text("aitest-secret\n", encoding="utf-8")
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "service_token_file": str(token_file),
                "aitest": {"read_token_file": str(token_file)},
            }
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.aitest.read_token is not None
    assert settings.aitest.read_token.get_secret_value() == "aitest-secret"


def test_missing_aitest_read_token_not_required(monkeypatch) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "test-service-token")
    settings = load_settings()
    assert settings.aitest.read_token is None  # dev 默认不强制；live e2e 用环境变量注入
