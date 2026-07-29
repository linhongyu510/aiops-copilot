"""Record auditable human alert-investigation timings without estimated values."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "manual_timing_cases.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def save(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def find(rows: list[dict], case_id: str) -> dict:
    row = next((item for item in rows if item["id"] == case_id), None)
    if row is None:
        raise SystemExit(f"unknown case: {case_id}")
    return row


def summary(rows: list[dict]) -> dict:
    finished = [row for row in rows if isinstance(row.get("duration_seconds"), (int, float))]
    durations = [float(row["duration_seconds"]) for row in finished]
    return {
        "case_count": len(rows),
        "finished_count": len(finished),
        "unfinished_count": len(rows) - len(finished),
        "average_minutes": round(statistics.mean(durations) / 60, 2) if durations else None,
        "median_minutes": round(statistics.median(durations) / 60, 2) if durations else None,
        "eligible_for_resume": 10 <= len(finished) <= 20,
        "note": "Only measured, human-completed cases are included.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["add", "start", "stop", "list", "summary"])
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--id")
    parser.add_argument("--title")
    parser.add_argument("--alert")
    parser.add_argument("--source-ref")
    parser.add_argument("--reviewer")
    parser.add_argument("--diagnosis")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    rows = load(args.dataset)

    if args.action == "add":
        if not all([args.id, args.title, args.alert, args.source_ref]):
            raise SystemExit("add requires --id --title --alert --source-ref")
        if any(row["id"] == args.id for row in rows):
            raise SystemExit(f"duplicate case: {args.id}")
        rows.append(
            {
                "id": args.id,
                "title": args.title,
                "alert": args.alert,
                "source_ref": args.source_ref,
                "reviewer": args.reviewer or "",
                "status": "pending",
            }
        )
        save(args.dataset, rows)
    elif args.action == "start":
        if not args.id or not args.reviewer:
            raise SystemExit("start requires --id and --reviewer")
        row = find(rows, args.id)
        if row.get("status") == "running":
            raise SystemExit("case is already running")
        row.update(
            reviewer=args.reviewer,
            status="running",
            started_at=datetime.now().astimezone().isoformat(),
            started_epoch=time.time(),
        )
        save(args.dataset, rows)
    elif args.action == "stop":
        if not args.id or not args.diagnosis or not args.evidence:
            raise SystemExit("stop requires --id --diagnosis --evidence")
        row = find(rows, args.id)
        if row.get("status") != "running" or "started_epoch" not in row:
            raise SystemExit("case was not started")
        ended = time.time()
        row.update(
            status="finished",
            ended_at=datetime.now().astimezone().isoformat(),
            duration_seconds=round(ended - float(row.pop("started_epoch")), 2),
            diagnosis=args.diagnosis,
            evidence=args.evidence,
        )
        save(args.dataset, rows)
    elif args.action == "list":
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    else:
        print(json.dumps(summary(rows), ensure_ascii=False, indent=2))
        return

    print(json.dumps(find(rows, args.id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
