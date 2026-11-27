# tests/unit/test_artifacts.py
import hashlib
import json
from pathlib import Path

from aitrace.cleaning.artifacts import ArtifactWriter

from aitrace.cleaning.slicer import Slice, Slices


def _slice(window: str, lines: tuple[str, ...]) -> Slice:
    return Slice("test", window, lines, sum(len(line.encode()) + 1 for line in lines))


def test_write_creates_round_dir_with_hashes(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(
        baseline=_slice("baseline", ("head line 1", "head line 2")),
        error_window=_slice("error_window", ("ERROR gpf",)),
        tail=_slice("tail", ("exit code 42",)),
    )
    summary = {
        "session_id": "S-1",
        "round_no": 1,
        "first_error": None,
        "fingerprints": {"stable": "abc"},
    }
    from datetime import UTC, datetime

    artifacts = writer.write(
        "S-1",
        1,
        summary=summary,
        slices=slices,
        metrics={"duration_ms": 12},
        now=datetime(2026, 11, 26, tzinfo=UTC),
    )
    evidence_dir = tmp_path / "evidence" / "S-1" / "round_1"
    assert evidence_dir.is_dir()
    for name in ("baseline.txt", "error_window.txt", "tail.txt", "summary.json", "metrics.json"):
        assert (evidence_dir / name).is_file()
    stored = json.loads((evidence_dir / "summary.json").read_text(encoding="utf-8"))
    assert stored["fingerprints"]["stable"] == "abc"
    # 每个切片文件的 hash 回填 referenced_logs
    assert len(artifacts.referenced_logs) == 3
    for entry in artifacts.referenced_logs:
        digest = hashlib.sha256((evidence_dir / entry["name"]).read_bytes()).hexdigest()
        assert entry["hash"] == digest
        assert entry["size_bytes"] > 0
        assert entry["source"] == "delivery"


def test_compression_ratio_metric(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(full=True)
    metrics = {
        "per_source": {"test": {"original_bytes": 1000, "slice_bytes": 100, "events_parsed": 7}}
    }
    artifacts = writer.write("S-2", 1, summary={}, slices=slices, metrics=metrics, now=None)  # type: ignore[arg-type]
    ratio = artifacts.metrics["per_source"]["test"]["compression_ratio"]
    assert ratio == 0.1


def test_overwrite_is_idempotent(tmp_path: Path) -> None:
    writer = ArtifactWriter(tmp_path)
    slices = Slices(full=True)
    first = writer.write("S-3", 1, summary={}, slices=slices, metrics={}, now=None)  # type: ignore[arg-type]
    second = writer.write("S-3", 1, summary={}, slices=slices, metrics={}, now=None)  # type: ignore[arg-type]
    assert first.paths == second.paths
    assert first.referenced_logs == second.referenced_logs
