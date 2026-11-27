import os
import subprocess
from pathlib import Path

import pytest

from aitrace.core.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "deploy/scripts").glob("*.sh"))


@pytest.mark.parametrize(
    "relative",
    [
        "deploy/systemd/aitrace.service",
        "deploy/logrotate/aitrace",
        "deploy/config/config.dev.yaml",
        "deploy/config/config.prod.example.yaml",
        "deploy/env/aitrace.env.example",
        "deploy/scripts/install_release.sh",
        "deploy/scripts/upgrade.sh",
        "deploy/scripts/rollback.sh",
        "deploy/scripts/migrate.sh",
        "deploy/scripts/healthcheck.sh",
    ],
)
def test_required_asset_exists(relative: str) -> None:
    assert (ROOT / relative).is_file()


def test_shell_scripts_parse() -> None:
    assert SCRIPTS
    for script in SCRIPTS:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_shell_scripts_are_executable() -> None:
    assert SCRIPTS
    for script in SCRIPTS:
        assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_unit_uses_release_entrypoint_and_tmpfs_home() -> None:
    text = (ROOT / "deploy/systemd/aitrace.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/aitrace/current/venv/bin/aitrace-server" in text
    assert "Environment=AITRACE_DATA_DIR=/var/lib/aitrace" in text
    assert "Environment=HOME=/run/aitrace/home" in text
    assert "RuntimeDirectory=aitrace" in text
    assert "TimeoutStopSec=120s" in text
    assert "User=aitrace" in text
    assert "User=root" not in text
    assert "AITRACE_SERVICE_TOKEN=" not in text


def test_prod_example_config_is_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    token_file = tmp_path / "service-token"
    token_file.write_text("example-token\n", encoding="utf-8")
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    settings = load_settings(ROOT / "deploy/config/config.prod.example.yaml")
    assert settings.env == "prod"
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "example-token"
    assert settings.storage.profile_domains_dir == Path(
        "/opt/aitrace/current/deploy/config/domains"
    )
