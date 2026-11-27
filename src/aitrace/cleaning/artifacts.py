from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aitrace.cleaning.slicer import Slice, Slices

_WINDOW_FILES = {
    "baseline": "baseline.txt",
    "error_window": "error_window.txt",
    "tail": "tail.txt",
}


@dataclass
class ArtifactSet:
    paths: dict[str, Path] = field(default_factory=dict)
    referenced_logs: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


class ArtifactWriter:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def write(
        self,
        session_id: str,
        round_no: int,
        *,
        summary: dict[str, Any],
        slices: Slices,
        metrics: dict[str, Any],
        now: datetime | None,
    ) -> ArtifactSet:
        round_dir = self._data_dir / "evidence" / session_id / f"round_{round_no}"
        round_dir.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactSet()
        slices_map: dict[str, Slice] = {}
        if slices.baseline:
            slices_map["baseline"] = slices.baseline
        if slices.error_window:
            slices_map["error_window"] = slices.error_window
        if slices.tail:
            slices_map["tail"] = slices.tail
        for window, name in _WINDOW_FILES.items():
            sl = slices_map.get(window)
            if sl is None:
                continue
            path = round_dir / name
            path.write_text("".join(line + "\n" for line in sl.lines), encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            artifacts.paths[window] = path
            artifacts.referenced_logs.append(
                {
                    "name": name,
                    "source": "delivery",
                    "events_parsed": metrics.get("per_source", {})
                    .get(sl.source, {})
                    .get("events_parsed", 0),
                    "size_bytes": path.stat().st_size,
                    "hash": digest,
                }
            )
        summary_path = round_dir / "summary.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifacts.paths["summary"] = summary_path
        final_metrics = _with_compression(metrics)
        metrics_path = round_dir / "metrics.json"
        metrics_path.write_text(
            json.dumps(final_metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        artifacts.paths["metrics"] = metrics_path
        artifacts.metrics = final_metrics
        return artifacts


def _with_compression(metrics: dict[str, Any]) -> dict[str, Any]:
    per_source: dict[str, Any] = {}
    for source, m in metrics.get("per_source", {}).items():
        original = m.get("original_bytes", 0)
        sliced = m.get("slice_bytes", 0)
        per_source[source] = {
            **m,
            "compression_ratio": round(sliced / original, 4) if original else 0.0,
        }
    return {**metrics, "per_source": per_source}
