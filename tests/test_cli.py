"""``aiops_core.cli`` 与 ``aiops_core.evidence.local_backends`` 的最小回归。

策略：直接调 ``main([...])``，捕获 stdout / stderr / exit code；不起子进程，测试可
离线、稳定、幂等。fixtures 位于 ``tests/fixtures/``。
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from aiops_core.cli import main
from aiops_core.evidence.local_backends import collect_local_evidence

FIXTURES = Path(__file__).parent / "fixtures"
ALERT_JSON = str(FIXTURES / "kafka_lag_alert.json")
EVIDENCE_JSON = str(FIXTURES / "kafka_lag_evidence.json")


@contextmanager
def _capture(argv: list[str]):
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:  # argparse 错误会抛 SystemExit
            code = int(e.code or 0)
    yield code, out.getvalue(), err.getvalue()


# --------------------------- CLI 子命令 ---------------------------


def test_cli_match_prints_top_k_json() -> None:
    with _capture(["match", ALERT_JSON, "--top-k", "2"]) as (code, out, _err):
        assert code == 0
        data = json.loads(out)
        assert data["labels"]["alertname"] == "KafkaConsumerLagHigh"
        assert data["matches"], "预期至少一条命中"
        assert data["matches"][0]["skill_id"] == "aiops.kafka_lag"


def test_cli_plan_uses_top_match_when_no_skill_id() -> None:
    with _capture(["plan", ALERT_JSON]) as (code, out, _err):
        assert code == 0
        plan = json.loads(out)
        assert plan["skill_id"] == "aiops.kafka_lag"
        assert plan["steps"], "预期 plan 至少有一步"


def test_cli_plan_unknown_skill_returns_error() -> None:
    with _capture(["plan", ALERT_JSON, "--skill-id", "not.exist"]) as (code, _out, err):
        assert code == 2
        assert "not.exist" in err


def test_cli_fingerprint_matches_reference() -> None:
    from aiops_core.triggers.fingerprint import alert_fingerprint

    with _capture(["fingerprint", ALERT_JSON]) as (code, out, _err):
        assert code == 0
        expected = alert_fingerprint(json.loads(Path(ALERT_JSON).read_text())["alerts"][0])
        assert out.strip() == expected


def test_cli_runbook_hits_known_wiki() -> None:
    with _capture(
        ["runbook", "kafka_consumer_lag", "--query", "Kafka 消费者滞后", "--top-k", "1"]
    ) as (code, out, _err):
        assert code == 0
        hits = json.loads(out)
        assert hits and hits[0]["doc_id"] == "kafka_consumer_lag"


def test_cli_env_returns_bool_map() -> None:
    with _capture(["env"]) as (code, out, _err):
        assert code == 0
        env = json.loads(out)
        assert set(env.keys()) == {"llm", "server", "rag", "state", "obs", "mcp"}


# --------------------------- oncall × 三种证据源 ---------------------------


def test_cli_oncall_none_evidence_renders_all_sections() -> None:
    with _capture(["oncall", ALERT_JSON]) as (code, out, _err):
        assert code == 0
        for anchor in (
            "# 诊断报告",
            "## 触发",
            "## 匹配到的 Skill",
            "## 执行计划",
            "## 证据摘要",
            "## 验收清单",
            "## Runbook 参考",
        ):
            assert anchor in out
        assert "未采集到证据" in out


def test_cli_oncall_file_evidence_appears_in_report() -> None:
    with _capture(
        [
            "oncall",
            ALERT_JSON,
            "--evidence",
            "file",
            "--evidence-file",
            EVIDENCE_JSON,
        ]
    ) as (code, out, _err):
        assert code == 0
        assert "consumergroup checkout-order 落后 orders 12345 条" in out
        assert "重新平衡耗时" in out


def test_cli_oncall_file_missing_arg_fails() -> None:
    with pytest.raises(SystemExit):
        main(["oncall", ALERT_JSON, "--evidence", "file"])


def test_cli_oncall_writes_to_output_file(tmp_path: Path) -> None:
    out_file = tmp_path / "report.md"
    with _capture(["oncall", ALERT_JSON, "--output", str(out_file)]) as (code, out, _err):
        assert code == 0
        assert out == "", "写文件模式下 stdout 应为空"
        assert out_file.is_file()
        content = out_file.read_text()
        assert "# 诊断报告" in content


# --------------------------- local backends ---------------------------


def test_local_backend_shell_allowlist_ok() -> None:
    step = {
        "step_id": "s1",
        "tool_hint": "shell",
        "tool_args_rendered": {"command": "echo hello-aiops"},
    }
    ev = collect_local_evidence(step)
    assert ev.status == "ok"
    assert "hello-aiops" in ev.summary


def test_local_backend_shell_denies_unlisted_bin() -> None:
    step = {
        "step_id": "s2",
        "tool_hint": "shell",
        "tool_args_rendered": {"command": "rm -rf /tmp"},
    }
    ev = collect_local_evidence(step)
    assert ev.status == "denied"
    assert "rm" in ev.error


def test_local_backend_read_file_missing() -> None:
    step = {
        "step_id": "s3",
        "tool_hint": "read_file",
        "tool_args_rendered": {"path": "/does/not/exist/path"},
    }
    ev = collect_local_evidence(step)
    assert ev.status == "missing"


def test_local_backend_read_file_ok(tmp_path: Path) -> None:
    p = tmp_path / "note.txt"
    p.write_text("hello wiki")
    step = {
        "step_id": "s4",
        "tool_hint": "read_file",
        "tool_args_rendered": {"path": str(p)},
    }
    ev = collect_local_evidence(step)
    assert ev.status == "ok"
    assert "hello wiki" in ev.summary


def test_local_backend_passthrough_when_no_local_impl() -> None:
    step = {
        "step_id": "s5",
        "tool_hint": "prom_query",
        "description": "查询指标",
    }
    ev = collect_local_evidence(step)
    assert ev.status == "skipped"
    assert ev.tool == "prom_query"


def test_local_backend_http_probe_reports_network_error() -> None:
    step = {
        "step_id": "s6",
        "tool_hint": "http_probe",
        "tool_args_rendered": {"url": "http://127.0.0.1:1/nope", "timeout": 0.3},
    }
    ev = collect_local_evidence(step)
    assert ev.status == "error"
    assert ev.error
