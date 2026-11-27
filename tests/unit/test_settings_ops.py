from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aitrace.core.settings import (
    BackupSettings,
    MonitorSettings,
    Settings,
    SettingsError,
    SloSettings,
    load_settings,
)


def test_slo_defaults_match_spec_initial_targets() -> None:
    s = SloSettings()
    assert s.ai_p95_baseline_seconds == 360
    assert s.queue_to_clean_p95_seconds == 120
    assert s.e2e_first_case_p95_seconds == 900
    assert s.e2e_queued_case_p95_seconds == 2700
    assert s.e2e_batch_p95_seconds == 3600
    assert s.fairness_max_gap_seconds == 720
    assert s.probe_max_age_minutes == 30


def test_slo_rejects_out_of_range_baseline() -> None:
    with pytest.raises(ValidationError):
        SloSettings(ai_p95_baseline_seconds=0)


def test_monitor_defaults_cover_alert_thresholds() -> None:
    m = MonitorSettings()
    assert m.check_interval_seconds == 60.0
    assert m.global_depth_warn_fraction == 0.8
    assert m.per_token_depth_warn == 100
    assert m.paused_auth_warn_seconds == 600
    assert m.ops_webhook is None


def test_backup_defaults() -> None:
    b = BackupSettings()
    assert b.keep_count == 3
    assert b.min_free_bytes == 4 * 1024**3
    assert b.remote_dest is None


def test_settings_gains_m4_domains_with_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "env: dev\n"
        "slo:\n  ai_p95_baseline_seconds: 420\n"
        "monitor:\n  check_interval_seconds: 30.0\n"
        "backup:\n  keep_count: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    settings = load_settings(config)
    assert settings.slo.ai_p95_baseline_seconds == 420
    assert settings.monitor.check_interval_seconds == 30.0
    assert settings.backup.keep_count == 5
    assert settings.release_root is None


def test_unknown_key_under_new_domains_is_rejected_with_chinese_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("env: dev\n" + "monitor:\n  not_a_key: 1\n", encoding="utf-8")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as ei:
        load_settings(config)
    assert "monitor" in str(ei.value) and "不支持的配置项" in str(ei.value)


def test_release_root_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    monkeypatch.setenv("AITRACE_RELEASE_ROOT", "/opt/aitrace/current")
    settings = Settings()
    assert settings.release_root == Path("/opt/aitrace/current")
