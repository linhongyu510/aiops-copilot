"""Generate resume-safe project numbers from machine-readable test evidence."""

from __future__ import annotations

import argparse
import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def _tool_count(project_root: Path) -> tuple[int, int]:
    mcp_count = 0
    for path in (project_root / "mcp_servers").glob("*_server.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                    for decorator in node.decorator_list
                ):
                    mcp_count += 1
    local_count = 0
    for path in (project_root / "app" / "tools").glob("*_tool.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local_count += sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                (isinstance(item, ast.Name) and item.id == "tool")
                or (
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Name)
                    and item.func.id == "tool"
                )
                for item in node.decorator_list
            )
            for node in ast.walk(tree)
        )
    return mcp_count, local_count


def generate(project_root: Path, junit: Path, coverage: Path, output: Path) -> dict:
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    coverage_data = json.loads(coverage.read_text(encoding="utf-8"))
    percent = round(float(coverage_data["totals"]["percent_covered"]), 2)
    mcp_count, local_count = _tool_count(project_root)
    evidence = {
        "tests": {
            "total": tests,
            "passed": tests - failures - errors - skipped,
            "failed": failures,
            "errors": errors,
            "skipped": skipped,
        },
        "coverage_percent": percent,
        "tools": {
            "mcp": mcp_count,
            "local": local_count,
            "total": mcp_count + local_count,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, default=Path("artifacts/pytest.xml"))
    parser.add_argument("--coverage", type=Path, default=Path("artifacts/coverage.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/project_evidence.json"))
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    print(json.dumps(generate(project_root, args.junit, args.coverage, args.output), indent=2))


if __name__ == "__main__":
    main()
