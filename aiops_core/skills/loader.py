"""Skill 加载器（零依赖版本）。

从 ``app.agent.skills.loader`` 抽出。原文件只依赖 stdlib + loguru，字面搬移，
仅把 ``from app.agent.skills.models import ...`` 换成 ``from aiops_core.skills.models``。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from aiops_core.skills.models import (
    SkillPack,
    SkillStep,
    SkillTrigger,
    SkillVerification,
)

# ------------------------- Legacy playbook -> Skill -------------------------


def playbook_to_skill(playbook: dict[str, Any], source: str = "legacy-playbook") -> SkillPack | None:
    """把旧的 playbook dict（symptoms+steps+verify）归一化为最小 SkillPack。"""
    playbook_id = str(playbook.get("playbook_id") or playbook.get("skill_id") or "").strip()
    title = str(playbook.get("title", "")).strip()
    if not playbook_id or not title:
        return None

    raw_steps = playbook.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        return None

    steps: list[SkillStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description") or raw.get("step") or "").strip()
        if not description:
            continue
        steps.append(
            SkillStep(
                id=str(raw.get("id") or f"step{index}"),
                description=description,
                tool_hint=raw.get("tool_hint"),
                tool_args_template=raw.get("tool_args_template") or {},
                depends_on=[str(dep) for dep in (raw.get("depends_on") or [])],
                optional=bool(raw.get("optional", False)),
                expected_output=raw.get("expected_output"),
            )
        )
    if not steps:
        return None

    trigger = SkillTrigger(
        symptoms=[str(item) for item in (playbook.get("symptoms") or []) if str(item).strip()],
        alert_names=[str(item) for item in (playbook.get("alert_names") or []) if str(item).strip()],
        label_selectors={
            str(k): str(v) for k, v in (playbook.get("label_selectors") or {}).items()
        },
    )

    verifications: list[SkillVerification] = []
    for raw in playbook.get("verifications") or []:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or "").strip()
        expr = str(raw.get("success_expr") or "").strip()
        if not tool or not expr:
            continue
        verifications.append(
            SkillVerification(
                tool=tool,
                args=raw.get("args") or {},
                success_expr=expr,
                description=str(raw.get("description", "")),
            )
        )

    return SkillPack(
        skill_id=playbook_id,
        title=title,
        description=str(playbook.get("root_cause_hint") or playbook.get("description") or ""),
        domain=str(playbook.get("domain") or "aiops"),
        trigger=trigger,
        required_tools=[str(item) for item in (playbook.get("required_tools") or [])],
        required_role=playbook.get("required_role") or "viewer",
        steps=steps,
        verifications=verifications,
        tags=[str(item) for item in (playbook.get("tags") or [])],
        review_status=playbook.get("review_status") or "demo",
        source=source,
    )


# ------------------------- 轻量 YAML 解析 -------------------------


def _parse_scalar(text: str) -> Any:
    text = text.strip()
    if not text:
        return ""
    if text.startswith("#"):
        return ""
    if "#" in text and not (text.startswith('"') or text.startswith("'")):
        text = text.split("#", 1)[0].rstrip()
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        body = text[1:-1]
        return (
            body.replace("\\\\", "\x00")
            .replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\x00", "\\")
        )
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text in {"null", "~", ""}:
        return None
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return text


def _dedent_level(line: str) -> int:
    stripped = line.lstrip(" ")
    return len(line) - len(stripped)


def _parse_yaml_dict(
    lines: list[str], indent: int, cursor: int
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while cursor < len(lines):
        raw = lines[cursor]
        if not raw.strip():
            cursor += 1
            continue
        line_indent = _dedent_level(raw)
        if line_indent < indent:
            break
        if line_indent > indent:
            cursor += 1
            continue
        stripped = raw.strip()
        if stripped.startswith("#"):
            cursor += 1
            continue
        if stripped.startswith("- "):
            break
        if ":" not in stripped:
            raise ValueError(f"YAML 解析失败：第 {cursor + 1} 行缺少冒号: {raw!r}")
        key, sep, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value:
            result[key] = _parse_scalar(value)
            cursor += 1
        else:
            cursor += 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor >= len(lines):
                result[key] = None
                continue
            next_line = lines[cursor]
            next_indent = _dedent_level(next_line)
            if next_indent <= indent:
                result[key] = None
                continue
            next_stripped = next_line.strip()
            if next_stripped.startswith("- "):
                nested, cursor = _parse_yaml_list(lines, next_indent, cursor)
            else:
                nested, cursor = _parse_yaml_dict(lines, next_indent, cursor)
            result[key] = nested
    return result, cursor


def _parse_yaml_list(
    lines: list[str], indent: int, cursor: int
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while cursor < len(lines):
        raw = lines[cursor]
        if not raw.strip():
            cursor += 1
            continue
        line_indent = _dedent_level(raw)
        if line_indent < indent:
            break
        if line_indent > indent:
            cursor += 1
            continue
        stripped = raw.strip()
        if not stripped.startswith("- "):
            break
        item_body = stripped[2:].strip()
        if not item_body:
            cursor += 1
            while cursor < len(lines) and not lines[cursor].strip():
                cursor += 1
            if cursor >= len(lines):
                result.append(None)
                continue
            next_indent = _dedent_level(lines[cursor])
            if next_indent <= indent:
                result.append(None)
                continue
            item, cursor = _parse_yaml_dict(lines, next_indent, cursor)
            result.append(item)
        elif ":" in item_body and not item_body.startswith(("{", "[")):
            item_indent = indent + 2
            virtual_lines = lines[:]
            virtual_lines[cursor] = " " * item_indent + item_body
            item, cursor = _parse_yaml_dict(virtual_lines, item_indent, cursor)
            result.append(item)
        else:
            result.append(_parse_scalar(item_body))
            cursor += 1
    return result, cursor


def parse_simple_yaml(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    parsed, _ = _parse_yaml_dict(lines, indent=0, cursor=0)
    return parsed


# ------------------------- 文件加载 -------------------------


def load_skill_file(path: Path) -> list[SkillPack]:
    if not path.exists() or not path.is_file():
        return []
    suffix = path.suffix.lower()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Skill 文件读取失败 {path}: {exc}")
        return []

    raw_items: list[dict[str, Any]] = []
    if suffix in {".yaml", ".yml"}:
        try:
            parsed = parse_simple_yaml(content)
        except Exception as exc:
            logger.warning(f"Skill YAML 解析失败 {path}: {exc}")
            return []
        if isinstance(parsed, dict) and parsed:
            raw_items.append(parsed)
    elif suffix in {".jsonl", ".ndjson"}:
        for lineno, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw_items.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.warning(f"Skill JSONL 第 {lineno} 行解析失败 {path}: {exc}")
    else:
        return []

    packs: list[SkillPack] = []
    source = f"file:{path.name}"
    for raw in raw_items:
        pack = _dict_to_skill(raw, source=source)
        if pack is not None:
            packs.append(pack)
    return packs


def _dict_to_skill(raw: dict[str, Any], source: str) -> SkillPack | None:
    if not isinstance(raw, dict):
        return None
    if "trigger" not in raw and ("symptoms" in raw or "playbook_id" in raw):
        return playbook_to_skill(raw, source=source)
    try:
        pack = SkillPack.model_validate(raw)
    except Exception as exc:
        logger.warning(f"Skill 校验失败（skill_id={raw.get('skill_id')!r}）: {exc}")
        return None
    pack.source = pack.source or source
    return pack


def load_skills_dir(directory: Path) -> list[SkillPack]:
    if not directory.exists() or not directory.is_dir():
        return []
    files: list[Path] = []
    for pattern in ("*.yaml", "*.yml", "*.jsonl", "*.ndjson"):
        files.extend(sorted(directory.rglob(pattern)))
    result: dict[str, SkillPack] = {}
    for path in files:
        for pack in load_skill_file(path):
            result[pack.skill_id] = pack
    return list(result.values())
