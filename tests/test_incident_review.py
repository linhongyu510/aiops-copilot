import json
from pathlib import Path

from evaluation.incident_review import export, load, validate


def _write_cases(path: Path, *, status: str, reviewer: str = "") -> None:
    path.write_text(
        json.dumps(
            {
                "id": "case-1",
                "source_kind": "synthetic_fault_fixture",
                "scenario": "dependency timeout",
                "signals": ["timeout"],
                "expected_actions": ["retry"],
                "review_status": status,
                "reviewer": reviewer,
                "review_notes": "",
                "contains_real_production_data": False,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_export_and_pending_claim_boundary(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    target = tmp_path / "review.csv"
    _write_cases(source, status="pending")

    export(source, target)
    result = validate(source)

    assert len(load(source)) == 1
    assert target.read_text(encoding="utf-8-sig").startswith("id,source_kind")
    assert result["pending"] == 1
    assert "synthetic fixtures only" in result["claim_boundary"]


def test_validate_requires_reviewer_and_approval(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    _write_cases(source, status="approved")

    result = validate(source, require_approved=True)

    assert result["errors"] == ["case-1: approved without reviewer"]


def test_validate_accepts_recorded_human_review(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    _write_cases(source, status="approved", reviewer="reviewer-id")

    result = validate(source, require_approved=True)

    assert result["approved"] == 1
    assert result["errors"] == []
