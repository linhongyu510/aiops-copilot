"""``python -m aiops_core`` 便捷入口。

只暴露内核层"决策类"能力：Skill 匹配 + Skill 计划 + Runbook 抽取 + 指纹 + 报告渲染。
完整 CLI（含证据源加载）属于 PR-4 的 ``aiops_core.cli``，本入口先作为 sanity check。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from aiops_core.planner.from_skill import plan_from_skill
from aiops_core.report.renderer import render_diagnosis_report
from aiops_core.skills import get_skill_registry
from aiops_core.skills.runbook_retriever import get_runbook_retriever
from aiops_core.triggers.fingerprint import alert_fingerprint
from aiops_core.triggers.normalize import extract_alerts, normalize_alert


def _load_json(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def _cmd_match(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alerts = extract_alerts(payload) or [payload]
    alert = alerts[0]
    normalized = normalize_alert(alert)
    matches = get_skill_registry().match(
        query=(normalized["title"] + " " + normalized["description"]).strip(),
        alert_labels=normalized["labels"],
        k=args.top_k,
    )
    result = [
        {
            "skill_id": m.skill.skill_id,
            "title": m.skill.title,
            "score": m.score,
            "reasons": list(m.reasons),
            "runbook_refs": list(m.skill.runbook_refs),
        }
        for m in matches
    ]
    _print_json(result)
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alerts = extract_alerts(payload) or [payload]
    alert = alerts[0]
    normalized = normalize_alert(alert)
    registry = get_skill_registry()
    if args.skill_id:
        pack = registry.get(args.skill_id)
        if pack is None:
            print(f"skill_id 不存在: {args.skill_id}", file=sys.stderr)
            return 2
        plan = plan_from_skill(pack, context={"labels": normalized["labels"]})
    else:
        matches = registry.match(
            query=(normalized["title"] + " " + normalized["description"]).strip(),
            alert_labels=normalized["labels"],
            k=1,
        )
        if not matches:
            print("未匹配到任何 Skill", file=sys.stderr)
            return 3
        plan = plan_from_skill(matches[0], context={"labels": normalized["labels"]})
    _print_json(plan)
    return 0


def _cmd_runbook(args: argparse.Namespace) -> int:
    retriever = get_runbook_retriever()
    sections = retriever.extract_relevant(
        doc_ids=args.doc_ids,
        query=args.query,
        top_k=args.top_k,
    )
    _print_json(
        [
            {
                "doc_id": s.doc_id,
                "doc_title": s.doc_title,
                "section_title": s.section_title,
                "content": s.content,
            }
            for s in sections
        ]
    )
    return 0


def _cmd_fingerprint(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alerts = extract_alerts(payload) or [payload]
    print(alert_fingerprint(alerts[0]))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    payload = _load_json(args.alert)
    alerts = extract_alerts(payload) or [payload]
    alert = alerts[0]
    normalized = normalize_alert(alert)
    registry = get_skill_registry()
    matches = registry.match(
        query=(normalized["title"] + " " + normalized["description"]).strip(),
        alert_labels=normalized["labels"],
        k=1,
    )
    skill_match_view = None
    plan_view: list[dict[str, Any]] = []
    if matches:
        plan_dict = plan_from_skill(matches[0], context={"labels": normalized["labels"]})
        skill_match_view = plan_dict
        plan_view = plan_dict["steps"]

    evidence: dict[str, Any] = {}
    if args.evidence_file:
        evidence = _load_json(args.evidence_file)

    trigger_view = {
        "title": normalized["title"],
        "description": normalized["description"],
        "labels": normalized["labels"],
        "starts_at": normalized["starts_at"],
        "fingerprint": alert_fingerprint(alert),
    }
    output = render_diagnosis_report(
        skill_match=skill_match_view,
        plan=plan_view,
        evidence_by_step=evidence,
        verifications=(skill_match_view or {}).get("verifications"),
        trigger=trigger_view,
    )
    sys.stdout.write(output)
    return 0


def _print_json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aiops_core")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_match = sub.add_parser("match", help="按告警 JSON 匹配 top-k Skill")
    p_match.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_match.add_argument("--top-k", type=int, default=3)
    p_match.set_defaults(func=_cmd_match)

    p_plan = sub.add_parser("plan", help="按告警或指定 skill_id 生成 PlanStep DAG")
    p_plan.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_plan.add_argument("--skill-id", default="", help="指定 skill_id，跳过匹配")
    p_plan.set_defaults(func=_cmd_plan)

    p_runbook = sub.add_parser("runbook", help="从 aiops-docs/ 抽取 h2 片段")
    p_runbook.add_argument("doc_ids", nargs="+", help="doc_id 列表（aiops-docs/<id>.md）")
    p_runbook.add_argument("--query", required=True)
    p_runbook.add_argument("--top-k", type=int, default=3)
    p_runbook.set_defaults(func=_cmd_runbook)

    p_fp = sub.add_parser("fingerprint", help="按旧版逻辑生成告警指纹")
    p_fp.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_fp.set_defaults(func=_cmd_fingerprint)

    p_report = sub.add_parser("report", help="渲染 markdown 诊断报告")
    p_report.add_argument("alert", help="Alertmanager 告警 JSON 文件路径")
    p_report.add_argument("--evidence-file", default="", help="回放证据 JSON 文件路径")
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
