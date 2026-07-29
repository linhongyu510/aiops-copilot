"""Generate a machine-readable claim boundary for evaluation labels."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def generate(output: Path) -> dict:
    rag_rows = _load(ROOT / "evaluation/datasets/rag_queries.jsonl")
    incident_rows = _load(ROOT / "evaluation/datasets/incident_cases.jsonl")
    alert_rows = _load(ROOT / "evaluation/datasets/anonymized_alerts.jsonl")

    def summarize(rows: list[dict]) -> dict:
        statuses = Counter(row.get("review_status", "unknown") for row in rows)
        sources = Counter(
            row.get("source_kind", row.get("label_origin", "unknown")) for row in rows
        )
        return {
            "rows": len(rows),
            "review_status": dict(statuses),
            "source_kind": dict(sources),
        }

    human_approved = sum(
        row.get("review_status") == "approved" for row in rag_rows + incident_rows + alert_rows
    )
    result = {
        "rag_queries": summarize(rag_rows),
        "synthetic_incidents": summarize(incident_rows),
        "anonymized_real_alerts": summarize(alert_rows),
        "human_approved_total": human_approved,
        "claim_boundary": {
            "may_claim_human_reviewed_labels": human_approved > 0,
            "may_claim_anonymized_real_alerts": bool(alert_rows),
            "allowed_current_wording": (
                "rule-generated reproducible evaluation set and synthetic fault fixtures"
            ),
            "blocked_wording": [
                "human-labeled dataset",
                "real production incident benchmark",
            ],
        },
        "remaining_external_input": (
            "An authorized source owner must supply pre-redacted alert rows and "
            "a named reviewer must approve labels; the repository cannot invent either."
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/human_review_status.json"),
    )
    args = parser.parse_args()
    print(json.dumps(generate(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
