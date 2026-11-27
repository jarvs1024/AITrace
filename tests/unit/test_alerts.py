from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aitrace.core.settings import Settings
from aitrace.observability.alerts import Alert, evaluate_alerts, next_alert_state, send_alerts


def _healthy() -> dict[str, Any]:
    return {
        "generated_at": "2026-02-01T00:00:00+00:00",
        "runtime": {
            "queue_counts": {
                "PENDING": 0,
                "CLEANING": 0,
                "READY": 0,
                "AI_RUNNING": 0,
                "WAITING": 0,
            },
            "paused_auth": {"count": 0, "sustained_seconds": 0.0},
            "waiting_timeouts": 0,
        },
        "waits": {},
        "fairness": {},
        "alerts_eligible": {
            "global_depth": 0,
            "per_token_depth": {},
            "per_token_depth_max": 0,
            "token_fetch_ok_300s": 100,
            "token_fetch_fail_300s": 5,
            "token_fetch_fail_rate": 0.05,
            "callback_ok_300s": 100,
            "callback_fail_300s": 2,
            "callback_fail_rate": 0.02,
            "callback_backlog_seconds": 0.0,
            "callback_drop_total": 0,
            "memory_materialize_fail_300s": 0,
            "fencing_reject_total": 0,
            "worker_utilization": 0.5,
            "waiting_timeouts": 0,
            "watermark_level": "NORMAL",
            "audit_verified": True,
        },
        "watermark": {"level": "NORMAL"},
        "callbacks": {"pending": 0, "backlog_seconds": 0.0},
        "workers": {"ai_running": 2, "width_w": 4, "utilization": 0.5},
        "audit": {"verified": True},
    }


_NOW = datetime(2026, 2, 1, tzinfo=UTC)


def _names(alerts: list[Alert]) -> list[str]:
    return [alert.name for alert in alerts]


def test_no_alerts_when_healthy(test_settings: Settings) -> None:
    assert evaluate_alerts(_healthy(), test_settings, now=_NOW) == []


def test_paused_auth_sustained_triggers_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["runtime"]["paused_auth"]["count"] = 1
    snapshot["runtime"]["paused_auth"]["sustained_seconds"] = 660
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "paused_auth_sustained" and a.severity == "WARN" for a in alerts), [
        a.name for a in alerts
    ]


def test_paused_auth_below_threshold_no_alert(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["runtime"]["paused_auth"]["count"] = 1
    snapshot["runtime"]["paused_auth"]["sustained_seconds"] = 599
    assert "paused_auth_sustained" not in _names(evaluate_alerts(snapshot, test_settings, now=_NOW))


def test_audit_chain_broken_is_critical(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["audit"]["verified"] = False
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "audit_chain_broken" and a.severity == "CRITICAL" for a in alerts), [
        a.name for a in alerts
    ]


def test_global_depth_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["global_depth"] = 401
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "global_depth_warn" and a.severity == "WARN" for a in alerts)
    # 阈值 500 * 0.8 = 400，当前 401
    match = next(a for a in alerts if a.name == "global_depth_warn")
    assert match.threshold == "400 条"
    assert match.current == "401 条"


def test_per_token_depth_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["per_token_depth"] = {"u1": 101, "u2": 50}
    snapshot["alerts_eligible"]["per_token_depth_max"] = 101
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    match = next(a for a in alerts if a.name == "per_token_depth_warn")
    assert match.severity == "WARN"
    assert match.current == "101 条"
    assert "u1" in match.message


def test_token_fetch_fail_rate_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["token_fetch_ok_300s"] = 80
    snapshot["alerts_eligible"]["token_fetch_fail_300s"] = 20
    snapshot["alerts_eligible"]["token_fetch_fail_rate"] = 0.2
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "token_fetch_fail_rate_warn" and a.severity == "WARN" for a in alerts)


def test_token_fetch_fail_rate_idle_no_alert(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["token_fetch_ok_300s"] = 0
    snapshot["alerts_eligible"]["token_fetch_fail_300s"] = 0
    snapshot["alerts_eligible"]["token_fetch_fail_rate"] = 0.0
    assert "token_fetch_fail_rate_warn" not in _names(
        evaluate_alerts(snapshot, test_settings, now=_NOW)
    )


def test_callback_fail_rate_and_backlog_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["callback_ok_300s"] = 90
    snapshot["alerts_eligible"]["callback_fail_300s"] = 10
    snapshot["alerts_eligible"]["callback_fail_rate"] = 0.1
    snapshot["alerts_eligible"]["callback_backlog_seconds"] = 601.0
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    names = _names(alerts)
    assert "callback_fail_rate_warn" in names
    assert "callback_backlog_warn" in names


def test_worker_util_low_only_with_pending_work(test_settings: Settings) -> None:
    idle = _healthy()
    idle["alerts_eligible"]["worker_utilization"] = 0.1
    idle["runtime"]["queue_counts"]["PENDING"] = 0
    idle["runtime"]["queue_counts"]["READY"] = 0
    assert "worker_util_low" not in _names(evaluate_alerts(idle, test_settings, now=_NOW))
    busy = _healthy()
    busy["alerts_eligible"]["worker_utilization"] = 0.1
    busy["runtime"]["queue_counts"]["READY"] = 3
    names = _names(evaluate_alerts(busy, test_settings, now=_NOW))
    assert "worker_util_low" in names


def test_worker_util_high_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["worker_utilization"] = 0.95
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "worker_util_high" and a.severity == "WARN" for a in alerts)


def test_watermark_forces_critical(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["watermark_level"] = "FORCE"
    snapshot["watermark"]["level"] = "FORCE"
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "watermark_force" and a.severity == "CRITICAL" for a in alerts)


def test_watermark_warn_severity(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["watermark_level"] = "WARN"
    snapshot["watermark"]["level"] = "WARN"
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "watermark_warn" and a.severity == "WARN" for a in alerts)


def test_waiting_timeouts_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["waiting_timeouts"] = 2
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    match = next(a for a in alerts if a.name == "waiting_timeouts")
    assert match.severity == "WARN"
    assert match.current == "2 个"


def test_memory_materialize_fail_warn(test_settings: Settings) -> None:
    snapshot = _healthy()
    snapshot["alerts_eligible"]["memory_materialize_fail_300s"] = 1
    alerts = evaluate_alerts(snapshot, test_settings, now=_NOW)
    assert any(a.name == "memory_materialize_fail" and a.severity == "WARN" for a in alerts)


def test_fencing_sustained_requires_growth_over_window(test_settings: Settings) -> None:
    t0 = datetime(2026, 2, 1, tzinfo=UTC)
    prev = {"fencing_count": 1, "fencing_started_at": None}
    grow = _healthy()
    grow["alerts_eligible"]["fencing_reject_total"] = 2
    first = evaluate_alerts(grow, test_settings, now=t0, prev_state=prev)
    assert "fencing_reject_sustained" not in _names(first)
    state = next_alert_state(grow, prev, t0)
    assert state["fencing_count"] == 2
    assert state["fencing_started_at"] == t0.timestamp()

    grow["alerts_eligible"]["fencing_reject_total"] = 5
    later = t0 + timedelta(seconds=test_settings.monitor.fencing_reject_warn_seconds + 1)
    second = evaluate_alerts(grow, test_settings, now=later, prev_state=state)
    assert any(a.name == "fencing_reject_sustained" and a.severity == "WARN" for a in second)


def test_fencing_sustained_resets_when_growth_stops(test_settings: Settings) -> None:
    t0 = datetime(2026, 2, 1, tzinfo=UTC)
    prev = {"fencing_count": 3, "fencing_started_at": t0.timestamp()}
    flat = _healthy()
    flat["alerts_eligible"]["fencing_reject_total"] = 3
    later = t0 + timedelta(seconds=test_settings.monitor.fencing_reject_warn_seconds + 1)
    alerts = evaluate_alerts(flat, test_settings, now=later, prev_state=prev)
    assert "fencing_reject_sustained" not in _names(alerts)
    state = next_alert_state(flat, prev, later)
    assert state["fencing_started_at"] is None


def test_send_alerts_posts_to_ops_webhook() -> None:
    posted: list[tuple[str, str]] = []
    send_alerts(
        [Alert(name="n", severity="WARN", threshold="t", current="c", message="中文")],
        ops_webhook="https://ops.example/hook",
        poster=lambda webhook, text: posted.append((webhook, text)),
    )
    assert [item[0] for item in posted] == ["https://ops.example/hook"]
    assert posted[0][1] == "【AITrace告警】WARN n：中文（阈值 t，当前 c）"


def test_send_alerts_no_webhook_is_no_op() -> None:
    calls: list[object] = []
    send_alerts(
        [Alert(name="n", severity="WARN", threshold="t", current="c", message="m")],
        ops_webhook=None,
        poster=lambda webhook, text: calls.append(text),
    )
    assert calls == []


def test_send_alerts_poster_exception_logs_warning(caplog: Any) -> None:
    def boom(webhook: str, text: str) -> None:
        raise RuntimeError("网络不可达")

    send_alerts(
        [Alert(name="n", severity="WARN", threshold="t", current="c", message="m")],
        ops_webhook="https://ops.example/hook",
        poster=boom,
    )
    assert "告警投递失败" in caplog.text
