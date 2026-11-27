"""进程内指标采集：计数器/直方图/仪表盘/滑动窗口计数。

V1 不使用 Prometheus（spec §7.3）：本模块是看板⑥实时监控台与告警巡检的
本地数据源，结构化日志任务是 SLS 聚合的长期载体。
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass
from typing import Any


class Counter:
    def __init__(self) -> None:
        self._value = 0

    def inc(self, n: int = 1) -> None:
        self._value += n

    def value(self) -> int:
        return self._value


@dataclass(frozen=True)
class HistogramSnapshot:
    count: int
    sum: float
    p50: float | None
    p95: float | None
    p99: float | None


class Histogram:
    """最近邻等级法分位（对真实排序样本，不落桶）。"""

    def __init__(self) -> None:
        self._values: list[float] = []
        self._sum = 0.0
        self._snapshot: HistogramSnapshot | None = None

    def observe(self, value: float) -> None:
        bisect.insort(self._values, value)
        self._sum += value
        self._snapshot = None

    def snapshot(self) -> HistogramSnapshot:
        if self._snapshot is None:
            self._snapshot = self._compute()
        return self._snapshot

    def _compute(self) -> HistogramSnapshot:
        n = len(self._values)
        if n == 0:
            return HistogramSnapshot(count=0, sum=0.0, p50=None, p95=None, p99=None)

        def rank(percentile: float) -> float:
            index = min(n, max(1, int(percentile / 100.0 * n + 0.999)))
            return self._values[index - 1]

        return HistogramSnapshot(
            count=n, sum=self._sum, p50=rank(50.0), p95=rank(95.0), p99=rank(99.0)
        )


class Gauge:
    def __init__(self) -> None:
        self._value = 0.0

    def set(self, value: float) -> None:
        self._value = value

    def add(self, delta: float) -> None:
        self._value += delta

    def value(self) -> float:
        return self._value


class WindowCounter:
    """按事件时间戳滑窗计数：现读现剪，供失败率/告警阈值判定。"""

    def __init__(self) -> None:
        self._events: list[float] = []

    def inc(self, n: int = 1) -> None:
        now = time.monotonic()
        self._events.extend([now] * n)

    def count_within(self, window_seconds: float) -> int:
        cutoff = time.monotonic() - window_seconds
        self._events = [t for t in self._events if t >= cutoff]
        return len(self._events)


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._gauges: dict[str, Gauge] = {}
        self._windows: dict[str, WindowCounter] = {}

    def counter(self, name: str) -> Counter:
        return self._counters.setdefault(name, Counter())

    def histogram(self, name: str) -> Histogram:
        return self._histograms.setdefault(name, Histogram())

    def gauge(self, name: str) -> Gauge:
        return self._gauges.setdefault(name, Gauge())

    def window_counter(self, name: str) -> WindowCounter:
        return self._windows.setdefault(name, WindowCounter())

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, c in sorted(self._counters.items()):
            out[name] = c.value()
        for name, h in sorted(self._histograms.items()):
            s = h.snapshot()
            out[name] = {
                "count": s.count,
                "sum": s.sum,
                "p50": s.p50,
                "p95": s.p95,
                "p99": s.p99,
            }
        for name, g in sorted(self._gauges.items()):
            out[name] = g.value()
        for name, w in sorted(self._windows.items()):
            out[f"{name}:count_60s"] = w.count_within(60.0)
            out[f"{name}:count_300s"] = w.count_within(300.0)
        return out
