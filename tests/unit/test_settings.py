# tests/unit/test_settings.py
from pathlib import Path

import pytest

from aitrace.core.settings import SettingsError, load_settings


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    monkeypatch.setenv("AITRACE_QUEUE__LEASE_SECONDS", "900")
    settings = load_settings(_write(tmp_path, "env: dev\nserver:\n  port: 9090\n"))
    assert settings.server.port == 9090
    assert settings.queue.lease_seconds == 900
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "s3cret"


def test_token_file_is_used_when_env_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "service-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    settings = load_settings(None)
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "file-token"


def test_missing_token_names_the_field_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN_FILE", raising=False)
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "env: dev\n"))
    message = str(caught.value)
    assert "配置校验失败" in message
    assert "服务令牌" in message


def test_unknown_key_is_rejected_with_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "queue:\n  lease_second: 900\n"))
    message = str(caught.value)
    assert "lease_second" in message
    assert "不支持的配置项" in message
    assert "lease_seconds" in message


def test_section_shape_error_names_section_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "server: 123\n"))
    message = str(caught.value)
    assert "服务端配置" in message
    assert "123" in message
    assert "必须是映射" in message
    assert "port" in message


def test_out_of_range_value_reports_current_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "server:\n  port: 70000\n"))
    message = str(caught.value)
    assert "服务端口" in message
    assert "70000" in message


def test_env_outranks_yaml_for_the_same_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    monkeypatch.setenv("AITRACE_SERVER__PORT", "9091")
    settings = load_settings(_write(tmp_path, "server:\n  port: 9090\n"))
    assert settings.server.port == 9091


def test_empty_token_file_is_rejected_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "service-token"
    token_file.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    with pytest.raises(SettingsError) as caught:
        load_settings(None)
    message = str(caught.value)
    assert "配置校验失败" in message
    assert "服务令牌文件" in message


def test_missing_config_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(tmp_path / "absent.yaml")
    assert "配置文件不存在" in str(caught.value)


def test_missing_token_file_names_field_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "absent-token"
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    with pytest.raises(SettingsError) as caught:
        load_settings(None)
    message = str(caught.value)
    assert "配置校验失败" in message
    assert "服务令牌文件（service_token_file）" in message
    assert str(token_file) in message
    assert "文件不存在" in message


def test_non_utf8_token_file_is_rejected_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "service-token"
    token_file.write_bytes(b"\xff\xfe\x00token")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    with pytest.raises(SettingsError) as caught:
        load_settings(None)
    message = str(caught.value)
    assert "服务令牌文件（service_token_file）" in message
    assert str(token_file) in message
    assert "UTF-8" in message


def test_token_never_leaks_in_repr_json_or_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "s3cret-token-value-9f3c"
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", token)
    settings = load_settings(_write(tmp_path, "env: dev\n"))
    assert token not in repr(settings)
    assert token not in settings.model_dump_json()

    for config in ("server:\n  port: 70000\n", "queue:\n  lease_second: 900\n", "server: 123\n"):
        with pytest.raises(SettingsError) as caught:
            load_settings(_write(tmp_path, config))
        assert token not in str(caught.value)


def test_token_from_file_never_leaks_in_repr_json_or_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "file-token-value-4a1d"
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "service-token"
    token_file.write_text(f"{token}\n", encoding="utf-8")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    settings = load_settings(None)
    assert token not in repr(settings)
    assert token not in settings.model_dump_json()

    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "server:\n  port: 70000\n"))
    assert token not in str(caught.value)


def test_non_mapping_config_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "- env: dev\n"))
    assert "顶层必须是映射" in str(caught.value)
