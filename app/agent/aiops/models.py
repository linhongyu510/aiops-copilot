"""结构化计划模型（P0.2）：PlanStep + 依赖归一化 + DAG 就绪批次调度。

计划从 list[str] 演进为带元数据的结构化步骤：
- LLM 输出层（PlanStep）：depends_on 使用 1-based 步骤序号，便于模型理解；
- 状态层（normalize_plan 产物）：普通 dict，step_id 形如 "s1"/"s2"，
  depends_on 引用 step_id —— 序号在步骤被弹出后会漂移，稳定 id 才能支撑
  Executor 的 DAG 并行调度与 checkpoint 恢复；
- 兼容层：纯字符串步骤仍被接受（等价于无依赖步骤），旧调用方/旧测试不破坏。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

MAX_PLAN_STEPS = 10


class PlanStep(BaseModel):
    """LLM 生成的单个计划步骤（结构化输出 schema）。"""

    description: str = Field(description="步骤描述：具体、可操作，说明要获取什么信息")
    tool_hint: str | None = Field(
        default=None,
        description="建议使用的工具名（可选）；executor 会结合工具路由自行决定",
    )
    expected_output: str | None = Field(
        default=None,
        description="预期产出（可选），如「CPU 使用率曲线」",
    )
    depends_on: list[int] = Field(
        default_factory=list,
        description="本步骤依赖的其他步骤序号（1-based，即「步骤1」填 1）。"
        "没有依赖必须填空列表——相互独立的步骤会被并行执行",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_plain_string(cls, value: Any) -> Any:
        """模型输出与旧调用方可能直接给纯字符串步骤，强转为最小 PlanStep。"""
        if isinstance(value, str):
            return {"description": value}
        return value


def _clean_description(raw: Any) -> str:
    return str(raw).strip() if raw is not None else ""


def _step_id(index: int) -> str:
    return f"s{index}"


def normalize_plan(steps: list[Any], id_offset: int = 0) -> list[dict[str, Any]]:
    """把 LLM 输出/旧格式步骤归一化为状态层 dict 列表。

    - 接受 str、dict、PlanStep；空描述步骤被丢弃；
    - 1-based 序号依赖转换为稳定 step_id；越界、自依赖被丢弃；
    - 超过 MAX_PLAN_STEPS 的尾部步骤被截断（与旧版 _parse_plan_steps 上限一致）；
    - id_offset：replanner 替换剩余计划时，新步骤 id 需要避开历史已完成 id
      （否则新 "s1" 会与已完成 "s1" 冲突，依赖判定错乱）；
    - 幂等：已是规范形态（含 step_id + depends_on 引用 id）的列表直接透传，
      防止 Executor 重复归一化时把字符串 id 依赖误当非法依赖丢弃。
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
            # 保留 Skill 化引入的扩展字段（skill_id / skill_step_id /
            # tool_args_template / optional），Executor/Replanner 需要它们做
            # 参数渲染和验收闭环。旧调用方不会带这些键，无副作用。
            for extra_key in ("skill_id", "skill_step_id", "tool_args_template", "optional"):
                if extra_key in step:
                    normalized_step[extra_key] = step[extra_key]
            result.append(normalized_step)
        return result

    normalized: list[dict[str, Any]] = []
    for raw in steps:
        if isinstance(raw, str):
            description, tool_hint, expected_output, raw_deps = raw, None, None, []
        elif isinstance(raw, PlanStep):
            description = raw.description
            tool_hint, expected_output = raw.tool_hint, raw.expected_output
            raw_deps = list(raw.depends_on)
        elif isinstance(raw, dict):
            description = _clean_description(raw.get("description") or raw.get("step"))
            tool_hint = raw.get("tool_hint")
            expected_output = raw.get("expected_output")
            raw_deps = list(raw.get("depends_on", []) or [])
        else:
            continue

        description = _clean_description(description)
        if not description:
            continue
        normalized.append(
            {
                "step_id": _step_id(len(normalized) + 1 + id_offset),
                "description": description,
                "tool_hint": tool_hint,
                "expected_output": expected_output,
                # 原始依赖先按位置记录，第二次遍历统一翻译成 step_id
                "_raw_deps": [
                    dep
                    for dep in raw_deps
                    if isinstance(dep, int) and not isinstance(dep, bool) and dep >= 1
                ],
            }
        )
        if len(normalized) >= MAX_PLAN_STEPS:
            break

    # 依赖翻译：序号 n → step_id "s{n}"；丢弃越界与自依赖
    total = len(normalized)
    for position, step in enumerate(normalized, start=1):
        step["depends_on"] = [
            _step_id(dep + id_offset)
            for dep in step.pop("_raw_deps")
            if 1 <= dep <= total and dep != position
        ]
    return normalized


def step_description(step: Any) -> str:
    """取步骤的可读描述（供 SSE 事件、报告引用）。"""
    if isinstance(step, str):
        return step
    if isinstance(step, dict):
        return str(step.get("description", ""))
    return str(getattr(step, "description", ""))


def step_task_text(step: dict[str, Any]) -> str:
    """Executor 的单步任务文本：描述 + 工具提示 + 预期产出。"""
    parts = [step_description(step)]
    tool_hint = step.get("tool_hint") if isinstance(step, dict) else None
    expected = step.get("expected_output") if isinstance(step, dict) else None
    if tool_hint:
        parts.append(f"（建议优先使用工具: {tool_hint}）")
    if expected:
        parts.append(f"（预期产出: {expected}）")
    return "".join(parts)


def step_id_of(step: Any, fallback: str = "") -> str:
    """取步骤的稳定 id；无 id 时回退 fallback（旧字符串步骤）。"""
    if isinstance(step, dict) and step.get("step_id"):
        return str(step["step_id"])
    return fallback


def ready_batch(
    plan: list[dict[str, Any]],
    completed_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """选出可执行批次：依赖全部完成的步骤，按计划顺序取前 limit 个。

    防死锁：若剩余步骤无一就绪（依赖环/悬空依赖），强制取第一个，
    保证图不会在 Executor 内空转卡死。
    """
    ready = [
        step
        for step in plan
        if not [dep for dep in step.get("depends_on", []) if dep not in completed_ids]
    ]
    if not ready and plan:
        ready = [plan[0]]
    return ready[: max(limit, 1)]
