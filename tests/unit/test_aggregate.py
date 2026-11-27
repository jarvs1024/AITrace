from datetime import UTC, datetime

from aitrace.scheduling.aggregate import (
    build_citation,
    cascade_rewrite,
    compute_batch_groups,
    passes_citation_threshold,
)

_STUB_CREATED_AT = datetime(2026, 11, 26, tzinfo=UTC)


class S:
    def __init__(
        self,
        session_id: str,
        stable_fp: str,
        batch_id: str = "B1",
        citation_group_id: str | None = None,
        created_at: datetime = _STUB_CREATED_AT,
    ) -> None:
        self.session_id = session_id
        self.stable_fingerprint = stable_fp
        self.batch_id = batch_id
        self.citation_group_id = citation_group_id
        self.created_at = created_at


def test_groups_by_stable_fingerprint_within_batch() -> None:
    sessions = [
        S("s1", "fp-a"),
        S("s2", "fp-a"),
        S("s3", "fp-b"),
        S("s4", "fp-a", batch_id="B2"),
    ]
    groups = compute_batch_groups(sessions)
    assert len(groups) == 3
    b1_a = next(g for g in groups if g.batch_id == "B1" and g.stable_fp == "fp-a")
    assert {s.session_id for s in b1_a.members} == {"s1", "s2"}
    assert b1_a.leader.session_id == "s1"


def test_threshold_requires_confidence_and_category() -> None:
    assert passes_citation_threshold(0.7, "FW") is True
    assert passes_citation_threshold(0.69, "FW") is False
    assert passes_citation_threshold(0.9, "OTHER") is False
    assert passes_citation_threshold(None, "FW") is False


def test_build_citation_shapes() -> None:
    cited = build_citation("s1", 1, "rp-1", 2, "CITING", None)
    assert cited == {
        "source_session": "s1",
        "source_round_no": 1,
        "source_report_id": "rp-1",
        "cited_version": 2,
        "citing_status": "CITING",
        "cascaded_from": None,
    }


def test_cascade_rewrite_stores_previous_source() -> None:
    citations = {
        "s2": {
            "source_session": "s1",
            "source_round_no": 1,
            "source_report_id": "rp-1",
            "cited_version": 1,
            "citing_status": "CITING",
            "cascaded_from": None,
        },
        "s3": {
            "source_session": "s1",
            "source_round_no": 1,
            "source_report_id": "rp-1",
            "cited_version": 1,
            "citing_status": "CITING",
            "cascaded_from": None,
        },
    }
    rewritten = cascade_rewrite(citations, new_source=("s9", 3, "rp-9"), cited_version=2)
    assert rewritten["s2"]["citing_status"] == "CITING"
    assert rewritten["s2"]["source_session"] == "s9"
    assert rewritten["s2"]["cascaded_from"]["source_session"] == "s1"
    assert rewritten["s2"]["cited_version"] == 2


def test_cascade_rewrite_keeps_stale_when_excluded() -> None:
    citations = {
        "s2": {
            "source_session": "s1",
            "source_round_no": 1,
            "source_report_id": "rp-1",
            "cited_version": 1,
            "citing_status": "CITING",
            "cascaded_from": None,
        }
    }
    rewritten = cascade_rewrite(
        citations, new_source=("s9", 3, "rp-9"), cited_version=2, excluded={"s2"}
    )
    assert rewritten["s2"]["citing_status"] == "CITING_STALE"
    assert rewritten["s2"]["source_session"] == "s1"
