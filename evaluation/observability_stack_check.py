"""Verify API → MCP tracing and centralized metrics.

The expected span names are derived from the probe response (`probe_tool`)
instead of being hardcoded, so this check works for any deployment
regardless of which read-only tool the probe happens to use.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx


async def verify(
    api_url: str,
    jaeger_url: str,
    prometheus_url: str,
    collector_url: str,
    grafana_url: str,
    output: Path,
) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        probe_response = await client.get(f"{api_url.rstrip('/')}/api/observability/trace-probe")
        probe_response.raise_for_status()
        probe = probe_response.json()
        trace_id = str(probe["trace_id"])

        # The Python BatchSpanProcessor exports on a timer; wait for the API
        # server span as well as the already completed downstream spans.
        await asyncio.sleep(6)
        trace_response = await client.get(f"{jaeger_url.rstrip('/')}/api/traces/{trace_id}")
        trace_response.raise_for_status()
        trace_data = trace_response.json()["data"][0]
        processes = trace_data["processes"]
        services = sorted({process["serviceName"] for process in processes.values()})
        operations = sorted({span["operationName"] for span in trace_data["spans"]})

        targets_response = await client.get(f"{prometheus_url.rstrip('/')}/api/v1/targets")
        targets_response.raise_for_status()
        targets = targets_response.json()["data"]["activeTargets"]
        up_targets = sorted(target["scrapeUrl"] for target in targets if target["health"] == "up")

        collector_response = await client.get(collector_url)
        collector_response.raise_for_status()
        grafana_response = await client.get(f"{grafana_url.rstrip('/')}/api/health")
        grafana_response.raise_for_status()
        grafana = grafana_response.json()

    probe_tool = probe.get("probe_tool", "")
    required_operations = {
        "GET /api/observability/trace-probe",
        f"mcp.{probe_tool}" if probe_tool else "mcp",
    }
    result = {
        "trace_id": trace_id,
        "declared_path": probe["path"],
        "services": services,
        "operations": operations,
        "span_count": len(trace_data["spans"]),
        "required_operations_present": sorted(required_operations.intersection(operations)),
        "prometheus_up_targets": up_targets,
        "collector_status": collector_response.json()["status"],
        "grafana_database": grafana["database"],
    }
    result["passed"] = all(
        (
            {"aiops-copilot", "aiops-mcp-ops"}.issubset(services),
            required_operations.issubset(operations),
            bool(up_targets),
            result["collector_status"] == "Server available",
            result["grafana_database"] == "ok",
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not result["passed"]:
        raise RuntimeError("observability stack verification failed")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:9900")
    parser.add_argument("--jaeger-url", default="http://127.0.0.1:16686")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--collector-url", default="http://127.0.0.1:13133")
    parser.add_argument("--grafana-url", default="http://127.0.0.1:3300")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/reliability/observability_stack.json"),
    )
    args = parser.parse_args()
    loop_factory = asyncio.SelectorEventLoop if hasattr(asyncio, "SelectorEventLoop") else None
    result = asyncio.run(
        verify(
            args.api_url,
            args.jaeger_url,
            args.prometheus_url,
            args.collector_url,
            args.grafana_url,
            args.output,
        ),
        loop_factory=loop_factory,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
