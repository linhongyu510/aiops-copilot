"""DAG 计划归一化 + 就绪批次调度（零依赖版本）。

从 ``app.agent.aiops.models`` 抽出。**只保留 dict-based 状态层**，不再暴露
``PlanStep`` pydantic 模型（那是 LLM 结构化输出用的，属于 app/ 层职责）。
"""

from __future__ import annotations

from typing import Any

MAX_PLAN_STEPS = 10


def _clean_description(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def _step_id(index: int) -> str:
    return f"s{index}"


def normalize_plan(steps: list[Any], id_offset: int = 0) -> list[dict[str, Any]]:
    """把 LLM 输出/旧格式步骤归一化为状态层 dict 列表。

    - 接受 str、dict；空描述步骤被丢弃；
    - 1-based 序号依赖转换为稳定 step_id；越界、自依赖被丢弃；
    - 超过 MAX_PLAN_STEPS 的尾部步骤被截断；
    - 已是规范形态（含 step_id + depends_on 引用 id）的列表直接透传，避免二次归一化。
    """
    if steps and all(
        isinstance(step, dict) and "step_id" in step and "depends_on" in step
        for step in steps
    ):
        result: list[dict[str, Any]] = []
        for step in steps:
            if not _clean_description(step.get("description")):
                continue
            normalized_step = {
                "step_id": str(step.get("step_id", "")),
                "description": _clean_description(step.get("description")),
                "tool_hint": step.get("tool_hint"),
                "expected_output": step.get("expected_output"),
                "depends_on": [str(dep) for dep in step.get("depends_on", []) if dep],
            }
            for extra_key in ("skill_id", "skill_step_id", "tool_args_template", "optional"):
                if extra_key in step:
                    normalized_step[extra_key] = step[extra_key]
            result.append(normalized_step)
        return result

    normalized: list[dict[str, Any]] = []
    for raw in steps:
        if isinstance(raw, str):
            description, tool_hint, expected_output, raw_deps = raw, None, None, []
        elif isinstance(raw, dict):
            description = _clean_description(raw.get("description") or raw.get("step"))
            tool_hint = raw.get("tool_hint")
            expected_output = raw.get("expected_output")
            raw_deps = list(raw.get("depends_on", []) or [])
        else:
            # LLM 结构化对象走 duck-typing（避免依赖 pydantic）
            description = _clean_description(getattr(raw, "description", ""))
            tool_hint = getattr(raw, "tool_hint", None)
            expected_output = getattr(raw, "expected_output", None)
            raw_deps = list(getattr(raw, "depends_on", []) or [])

        description = _clean_description(description)
        if not description:
            continue
        normalized.append(
            {
                "step_id": _step_id(len(normalized) + 1 + id_offset),
                "description": description,
                "tool_hint": tool_hint,
                "expected_output": expected_output,
                "_raw_deps": [
                    dep
                    for dep in raw_deps
                    if isinstance(dep, int) and not isinstance(dep, bool) and dep >= 1
                ],
            }
        )
        if len(normalized) >= MAX_PLAN_STEPS:
            break

    total = len(normalized)
    for position, step in enumerate(normalized, start=1):
        step["depends_on"] = [
            _step_id(dep + id_offset)
            for dep in step.pop("_raw_deps")
            if 1 <= dep <= total and dep != position
        ]
    return normalized


def step_description(step: Any) -> str:
    if isinstance(step, str):
        return step
    if isinstance(step, dict):
        return str(step.get("description", ""))
    return str(getattr(step, "description", ""))


def step_id_of(step: Any, fallback: str = "") -> str:
    if isinstance(step, dict) and step.get("step_id"):
        return str(step["step_id"])
    return fallback


def ready_batch(
    plan: list[dict[str, Any]],
    completed_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """选出可执行批次：依赖全部完成的步骤，按计划顺序取前 limit 个。

    防死锁：若剩余步骤无一就绪（依赖环/悬空依赖），强制取第一个。
    """
    ready = [
        step
        for step in plan
        if not [dep for dep in step.get("depends_on", []) if dep not in completed_ids]
    ]
    if not ready and plan:
        ready = [plan[0]]
    return ready[: max(limit, 1)]
