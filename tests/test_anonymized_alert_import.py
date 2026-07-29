import csv
from pathlib import Path

import pytest

from evaluation.import_anonymized_alerts import import_alerts


def _write_csv(path: Path, summary: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "occurred_at",
                "service_alias",
                "severity",
                "summary",
                "signals",
                "resolution",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "occurred_at": "2026-07-01T10:00:00+08:00",
                "service_alias": "feed-service-a",
                "severity": "P1",
                "summary": summary,
                "signals": "5xx_high|dependency_timeout",
                "resolution": "rollback and verify",
            }
        )


def test_import_anonymized_alert_requires_authorization(tmp_path: Path) -> None:
    source = tmp_path / "alerts.csv"
    _write_csv(source, "5xx increased after rollout")

    with pytest.raises(ValueError, match="acknowledgement"):
        import_alerts(
            source,
            tmp_path / "out.jsonl",
            authorized_source_acknowledged=False,
        )


def test_import_anonymized_alert_rejects_direct_identifiers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alerts.csv"
    _write_csv(source, "owner user@example.com saw 10.1.2.3")

    with pytest.raises(ValueError, match="sensitive"):
        import_alerts(
            source,
            tmp_path / "out.jsonl",
            authorized_source_acknowledged=True,
        )


def test_import_anonymized_alert_creates_pending_review_record(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alerts.csv"
    output = tmp_path / "out.jsonl"
    _write_csv(source, "5xx increased after rollout")

    rows = import_alerts(
        source,
        output,
        authorized_source_acknowledged=True,
    )

    assert rows[0]["source_kind"] == "anonymized_real_alert"
    assert rows[0]["review_status"] == "pending"
    assert rows[0]["contains_raw_production_data"] is False
    assert output.exists()


def test_import_anonymized_alert_rejects_invalid_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "alerts.csv"
    _write_csv(source, "5xx increased after rollout")
    text = source.read_text(encoding="utf-8").replace(
        "2026-07-01T10:00:00+08:00",
        "not-a-time",
    )
    source.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="invalid occurred_at"):
        import_alerts(
            source,
            tmp_path / "out.jsonl",
            authorized_source_acknowledged=True,
        )
