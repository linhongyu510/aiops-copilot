"""Skill 参数模板渲染（零依赖版本）。

从 ``app.agent.skills.renderer`` 抽出，字面复制。原实现已用手写正则替换，无 jinja2 依赖。
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def _resolve(path: str, context: dict[str, Any]) -> Any | None:
    if not path:
        return None
    parts = [segment.strip() for segment in path.split(".") if segment.strip()]
    cursor: Any = context
    for part in parts:
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    return cursor


def render_string(text: str, context: dict[str, Any]) -> str:
    """把字符串里的占位符替换为 context 值；未命中占位保留原样。"""
    if "{{" not in text:
        return text

    def _replace(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        candidates: list[str] = [raw]
        if raw.startswith("context."):
            candidates.append(raw[len("context."):])
        elif raw != "input" and "." not in raw:
            candidates.append(f"context.{raw}")
        for path in candidates:
            value = _resolve(path, context)
            if value is not None:
                return str(value)
        logger.debug(f"参数模板未命中占位符: {{{{ {raw} }}}}")
        return match.group(0)

    return _PLACEHOLDER.sub(_replace, text)


def render_args(template: Any, context: dict[str, Any]) -> Any:
    if isinstance(template, str):
        return render_string(template, context)
    if isinstance(template, dict):
        return {key: render_args(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [render_args(item, context) for item in template]
    return template


def build_render_context(
    input_text: str,
    past_steps: list[tuple[str, str]] | None = None,
    step_id_to_result: dict[str, str] | None = None,
    skill_context: dict[str, Any] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "input": input_text,
        "labels": dict(labels or {}),
        "step": dict(step_id_to_result or {}),
    }
    if skill_context:
        for key, value in skill_context.items():
            if key in {"input", "step", "labels"}:
                continue
            context[key] = value
    return context
