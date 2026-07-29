"""Export, import, and validate the human label-review workflow."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_JSONL = ROOT / "evaluation" / "datasets" / "rag_queries.jsonl"
DEFAULT_CSV = ROOT / "evaluation" / "datasets" / "rag_queries_review.csv"

FIELDS = [
    "id",
    "query",
    "expected_sources",
    "expected_keywords",
    "category",
    "style",
    "difficulty",
    "split",
    "label_origin",
    "review_status",
    "reviewer",
    "review_notes",
]


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def export_csv(jsonl_path: Path, csv_path: Path) -> None:
    rows = load_jsonl(jsonl_path)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            exported = {field: row.get(field, "") for field in FIELDS}
            exported["expected_sources"] = "|".join(row.get("expected_sources", []))
            exported["expected_keywords"] = "|".join(row.get("expected_keywords", []))
            writer.writerow(exported)
    print(f"exported {len(rows)} rows to {csv_path}")


def import_csv(csv_path: Path, jsonl_path: Path) -> None:
    rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            row["expected_sources"] = [
                item.strip() for item in row["expected_sources"].split("|") if item.strip()
            ]
            row["expected_keywords"] = [
                item.strip() for item in row["expected_keywords"].split("|") if item.strip()
            ]
            rows.append(row)
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"imported {len(rows)} rows to {jsonl_path}")


def validate(jsonl_path: Path) -> int:
    rows = load_jsonl(jsonl_path)
    ids = [row.get("id") for row in rows]
    errors: list[str] = []
    if len(ids) != len(set(ids)):
        errors.append("duplicate ids")
    for row in rows:
        if not row.get("query"):
            errors.append(f"{row.get('id')}: empty query")
        if not row.get("expected_sources"):
            errors.append(f"{row.get('id')}: missing expected_sources")
        for source in row.get("expected_sources", []):
            if not (ROOT / "aiops-docs" / source).exists():
                errors.append(f"{row.get('id')}: source not found: {source}")
    statuses = Counter(row.get("review_status", "unknown") for row in rows)
    print(
        json.dumps(
            {"rows": len(rows), "review_status": statuses, "errors": errors[:20]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if errors else 0


def precheck(jsonl_path: Path) -> int:
    """Machine-check structural labels without claiming human approval."""
    rows = load_jsonl(jsonl_path)
    issues: list[dict] = []
    for row in rows:
        source_text = "\n".join(
            (ROOT / "aiops-docs" / source).read_text(encoding="utf-8")
            for source in row.get("expected_sources", [])
            if (ROOT / "aiops-docs" / source).exists()
        ).lower()
        missing = [
            keyword
            for keyword in row.get("expected_keywords", [])
            if keyword.lower() not in source_text
        ]
        if missing:
            issues.append({"id": row.get("id"), "keywords_missing_from_source": missing})
    result = {
        "rows": len(rows),
        "machine_precheck_passed": len(rows) - len(issues),
        "machine_precheck_failed": len(issues),
        "human_approved": sum(row.get("review_status") == "approved" for row in rows),
        "issues": issues[:50],
        "note": "Machine precheck is not a substitute for human review.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if issues else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["export", "import", "validate", "precheck"])
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    args = parser.parse_args()
    if args.action == "export":
        export_csv(args.jsonl, args.csv)
    elif args.action == "import":
        import_csv(args.csv, args.jsonl)
    elif args.action == "validate":
        raise SystemExit(validate(args.jsonl))
    else:
        raise SystemExit(precheck(args.jsonl))


if __name__ == "__main__":
    main()
