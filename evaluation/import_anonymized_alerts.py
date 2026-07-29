"""Import authorized, pre-redacted alert samples without retaining raw IDs."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

REQUIRED_FIELDS = (
    "occurred_at",
    "service_alias",
    "severity",
    "summary",
    "signals",
    "resolution",
)
SENSITIVE_PATTERNS = {
    "email": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "credential": re.compile(
        r"\b(?:sk-|AKID|Bearer\s+)[A-Za-z0-9._-]{8,}",
        re.I,
    ),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
}
ALLOWED_SEVERITIES = {"p0", "p1", "p2", "p3", "p4", "critical", "high", "medium", "low", "info"}


def _sensitive_hits(row: dict[str, str]) -> list[str]:
    text = " ".join(str(value) for value in row.values())
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(text)]


def import_alerts(
    input_path: Path,
    output_path: Path,
    *,
    authorized_source_acknowledged: bool,
) -> list[dict]:
    if not authorized_source_acknowledged:
        raise ValueError("authorized source acknowledgement is required")
    with input_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_fields = [
            field for field in REQUIRED_FIELDS if field not in (reader.fieldnames or [])
        ]
        if missing_fields:
            raise ValueError(f"missing fields: {missing_fields}")
        rows = list(reader)

    imported: list[dict] = []
    for index, row in enumerate(rows, start=1):
        empty_fields = [field for field in REQUIRED_FIELDS if not row[field].strip()]
        if empty_fields:
            raise ValueError(f"row {index} has empty fields: {empty_fields}")
        hits = _sensitive_hits(row)
        if hits:
            raise ValueError(f"row {index} contains sensitive patterns: {hits}")
        try:
            datetime.fromisoformat(row["occurred_at"].strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"row {index} has invalid occurred_at") from exc
        severity = row["severity"].strip().lower()
        if severity not in ALLOWED_SEVERITIES:
            raise ValueError(
                f"row {index} has unsupported severity; "
                f"expected one of {sorted(ALLOWED_SEVERITIES)}"
            )
        normalized = json.dumps(
            {field: row[field].strip() for field in REQUIRED_FIELDS},
            ensure_ascii=False,
            sort_keys=True,
        )
        imported.append(
            {
                "id": f"alert-{hashlib.sha256(normalized.encode()).hexdigest()[:12]}",
                "source_kind": "anonymized_real_alert",
                "occurred_at": row["occurred_at"].strip(),
                "service_alias": row["service_alias"].strip(),
                "severity": severity,
                "scenario": row["summary"].strip(),
                "signals": [item.strip() for item in row["signals"].split("|") if item.strip()],
                "resolution": row["resolution"].strip(),
                "review_status": "pending",
                "reviewer": "",
                "review_notes": "",
                "contains_raw_production_data": False,
                "deidentification": (
                    "source owner acknowledged authorization; importer rejected "
                    "common direct identifier and credential patterns"
                ),
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in imported),
        encoding="utf-8",
    )
    return imported


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evaluation/datasets/anonymized_alerts.jsonl"),
    )
    parser.add_argument(
        "--acknowledge-authorized-source",
        action="store_true",
        help="Confirm the source owner authorized de-identified evaluation use.",
    )
    args = parser.parse_args()
    rows = import_alerts(
        args.input,
        args.output,
        authorized_source_acknowledged=args.acknowledge_authorized_source,
    )
    print(
        json.dumps(
            {
                "imported": len(rows),
                "output": str(args.output),
                "review_status": "pending",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
