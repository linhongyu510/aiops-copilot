"""Capture normal, partial-503 and unreachable WINDOS evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mcp_servers.ops_server as ops
from app.reliability import dependency_guards


async def _reset_client() -> None:
    if ops._windos_client is not None:
        await ops._windos_client.aclose()
    ops._windos_client = None
    ops._windos_client_signature = None
    dependency_guards.reset()


async def capture(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    original_url = os.getenv("WINDOS_BASE_URL")
    normal = await ops.windos_diagnose_overview.fn()

    original_get = ops._windos_get

    async def one_503(path: str, parameters=None):
        if path == "/api/v1/agent/metrics/tools":
            return {
                "source": "fault_injection",
                "endpoint": path,
                "read_only": True,
                "http_status": 503,
                "ok": False,
                "data": {"detail": "synthetic_single_endpoint_unavailable"},
            }
        return await original_get(path, parameters)

    ops._windos_get = one_503
    try:
        partial = await ops.windos_diagnose_overview.fn()
    finally:
        ops._windos_get = original_get

    try:
        os.environ["WINDOS_BASE_URL"] = "http://127.0.0.1:1"
        await _reset_client()
        unreachable = await ops.windos_diagnose_overview.fn()
    finally:
        if original_url is None:
            os.environ.pop("WINDOS_BASE_URL", None)
        else:
            os.environ["WINDOS_BASE_URL"] = original_url
        await _reset_client()

    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "scenarios": {
            "normal_live": normal,
            "single_endpoint_503_fault_injection": partial,
            "windos_unreachable_fault_injection": unreachable,
        },
    }
    (output_dir / "windos_scenarios.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# WINDOS 联动三场景记录", "", f"- 时间：{report['captured_at']}", ""]
    for name, data in report["scenarios"].items():
        evidence = data["evidence"]
        ok_count = sum(item.get("ok", False) for item in evidence.values())
        lines.extend(
            [
                f"## {name}",
                "",
                f"- 成功证据：{ok_count}/{len(evidence)}",
                f"- 自动变更：{data['automatic_changes_permitted']}",
                "- 端点状态："
                + "、".join(
                    f"{key}={value.get('http_status', value.get('error_type', 'unknown'))}"
                    for key, value in evidence.items()
                ),
                "",
            ]
        )
    (output_dir / "windos_scenarios.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/reliability"))
    args = parser.parse_args()
    asyncio.run(capture(args.output_dir))
    print(args.output_dir / "windos_scenarios.json")


if __name__ == "__main__":
    main()
