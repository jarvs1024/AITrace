from __future__ import annotations

import json
import logging

import pytest

from aitrace.observability.logging import configure_logging, log_event
from aitrace.observability.metrics import Histogram, MetricsRegistry
from aitrace.observability.pipeline import record_stage_event


def test_histogram_percentiles_nearest_rank() -> None:
    h = Histogram()
    for v in [0.5, 1.2, 8.0, 20.0, 100.0, 100.0, 400.0, 1000.0]:
        h.observe(v)
    s = h.snapshot()
    assert s.count == 8
    assert s.sum == 1629.7
    assert s.p50 == 20.0
    assert s.p95 == 1000.0
    assert s.p99 == 1000.0


def test_histogram_single_value() -> None:
    h = Histogram()
    h.observe(7.5)
    s = h.snapshot()
    assert (s.count, s.p50, s.p95, s.p99) == (1, 7.5, 7.5, 7.5)


def test_histogram_empty_snapshot_is_neutral() -> None:
    s = Histogram().snapshot()
    assert s.count == 0
    assert s.p50 is None and s.p95 is None and s.p99 is None


def test_window_counter_counts_within_window() -> None:
    reg = MetricsRegistry()
    c = reg.window_counter("x")
    for _ in range(10):
        c.inc()
    snap = reg.snapshot()
    assert snap["x:count_60s"] == 10
    assert snap["x:count_300s"] == 10


def test_record_stage_event_emits_metrics_and_log(caplog: pytest.LogCaptureFixture) -> None:
    reg = MetricsRegistry()
    with caplog.at_level(logging.INFO):
        record_stage_event(
            reg,
            task_type="case_ai",
            queue_key="u1",
            from_state="READY",
            to_state="AI_RUNNING",
            duration_seconds=12.5,
        )
    metric = reg._histograms["task_stage_duration_seconds"].snapshot()
    assert metric.count == 1
    assert metric.sum == 12.5
    text = caplog.text
    assert "task_stage" in text
    assert "duration_seconds" in text
    assert "queue_key" in text


def test_record_stage_event_log_line_is_json_lines(caplog: pytest.LogCaptureFixture) -> None:
    reg = MetricsRegistry()
    with caplog.at_level(logging.INFO):
        record_stage_event(
            reg,
            task_type="batch_common",
            queue_key="u2",
            from_state="PENDING",
            to_state="CLEANING",
            duration_seconds=1.0,
        )
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["event"] == "task_stage"
    assert payload["component"] == "pipeline"
    assert payload["from_state"] == "PENDING"
    assert payload["to_state"] == "CLEANING"


def test_log_event_masks_token_like_fields(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging("INFO")
    with caplog.at_level(logging.INFO):
        log_event(
            "ai",
            "token_fetched",
            level="INFO",
            token="secret-token-value",
            access_token="another-secret",
            batch_id="B1",
        )
    assert "secret-token-value" not in caplog.text
    assert "another-secret" not in caplog.text
    assert "<已脱敏>" in caplog.text


def test_log_event_level_in_payload(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        log_event("probe", "probe_done", level="WARNING", exit_code=0)
    payload = json.loads(caplog.records[-1].getMessage())
    assert payload["level"] == "WARNING"
    assert payload["component"] == "probe"
    assert payload["event"] == "probe_done"
