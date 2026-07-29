"""Export and validate incident-case human review without inventing provenance."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = ROOT / "evaluation" / "datasets" / "incident_cases.jsonl"
DEFAULT_CSV = ROOT / "evaluation" / "datasets" / "incident_cases_review.csv"
FIELDS = [
    "id",
    "source_kind",
    "scenario",
    "signals",
    "expected_actions",
    "review_status",
    "reviewer",
    "review_notes",
    "contains_real_production_data",
]


def load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def export(path: Path, csv_path: Path) -> None:
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in load(path):
            copy = dict(row)
            copy["signals"] = "|".join(row["signals"])
            copy["expected_actions"] = "|".join(row["expected_actions"])
            writer.writerow(copy)


def validate(path: Path, require_approved: bool = False) -> dict:
    rows = load(path)
    errors: list[str] = []
    for row in rows:
        if row["review_status"] == "approved" and not row["reviewer"].strip():
            errors.append(f"{row['id']}: approved without reviewer")
        if row["contains_real_production_data"]:
            errors.append(f"{row['id']}: production data must be anonymized before import")
        if require_approved and row["review_status"] != "approved":
            errors.append(f"{row['id']}: human review pending")
    return {
        "rows": len(rows),
        "approved": sum(row["review_status"] == "approved" for row in rows),
        "pending": sum(row["review_status"] == "pending" for row in rows),
        "errors": errors,
        "claim_boundary": (
            "synthetic fixtures only; do not describe as real or human-reviewed incidents"
            if any(row["review_status"] != "approved" for row in rows)
            else "human review recorded; source_kind still controls real/synthetic wording"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["export", "validate"])
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--require-approved", action="store_true")
    args = parser.parse_args()
    if args.action == "export":
        export(args.jsonl, args.csv)
        print(args.csv)
    else:
        result = validate(args.jsonl, args.require_approved)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(bool(result["errors"]))


if __name__ == "__main__":
    main()
