from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_percentile_nearest_rank() -> None:
    from tools.perf_harness import percentile  # 通过 sys.path 注入 tools

    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([1, 2, 3, 4, 5], 50) == 3


def test_bucket_waits_by_predecessor_depth() -> None:
    from tools.perf_harness import bucket_waits

    waits = [("u1", 0, 10.0), ("u1", 1, 30.0), ("u2", 0, 5.0)]
    buckets = bucket_waits(waits)  # list[(queue_key, depth, wait_seconds)] → {"0": {...}}
    assert buckets["0"]["p95"] >= 5.0


def test_fairness_gap_within_bucket() -> None:
    from tools.perf_harness import fairness_gap

    gap = fairness_gap({("u1", "0"): [10, 20], ("u2", "0"): [5, 7]}, bucket="0")
    assert gap == 13.0


def test_summarize_against_slo_targets() -> None:
    from tools.perf_harness import SloSettings, summarize

    perf_metrics_dict: dict = {}
    results = summarize(perf_metrics_dict, SloSettings())  # 返回 [(指标, 目标, 实测, ok)]
    assert all(isinstance(r.ok, bool) for r in results)


def test_summarize_marks_over_target_as_miss() -> None:
    from tools.perf_harness import SloSettings, summarize

    results = summarize({"accept": {"p95": 39.337}}, SloSettings())
    accept_row = next(r for r in results if r.metric.startswith("投递受理确认"))
    assert accept_row.ok is False
    assert accept_row.measured == 39.337


def test_summarize_marks_under_target_as_pass() -> None:
    from tools.perf_harness import SloSettings, summarize

    results = summarize({"accept": {"p95": 1.5}}, SloSettings())
    accept_row = next(r for r in results if r.metric.startswith("投递受理确认"))
    assert accept_row.ok is True


def test_summarize_skips_fairness_rows_without_metrics() -> None:
    from tools.perf_harness import SloSettings, summarize

    results = summarize({"accept": {"p95": 1.5}}, SloSettings())
    assert not any(r.metric.startswith("跨用户公平性") for r in results)


def test_summarize_includes_fairness_rows_when_present() -> None:
    from tools.perf_harness import SloSettings, summarize

    results = summarize({"fairness": {"0": 13.0, "1-2": 0.0}}, SloSettings())
    fairness_rows = [r for r in results if r.metric.startswith("跨用户公平性")]
    assert len(fairness_rows) == 3
    assert fairness_rows[0].ok is True
    assert fairness_rows[2].measured is None
