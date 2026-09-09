"""Skill Pack 数据模型（零依赖版本）。

从 ``app.agent.skills.models`` 抽出。原文件不依赖 ``app.*``，字面复制。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillTrigger(BaseModel):
    """Skill 触发条件：多信号联合评分，任一信号命中即视为候选。"""

    symptoms: list[str] = Field(
        default_factory=list,
        description="触发关键词/短语；用于 TF-IDF 语义匹配",
    )
    alert_names: list[str] = Field(
        default_factory=list,
        description="精确匹配 Alertmanager labels.alertname；命中直接给高分",
    )
    label_selectors: dict[str, str] = Field(
        default_factory=dict,
        description="告警 labels 正则匹配（key=label_name，value=正则）；全部命中才算命中",
    )
    min_score: float = Field(
        0.15,
        description="symptoms 语义相似度阈值；低于此值不视为候选",
        ge=0.0,
        le=1.0,
    )


class SkillPrecondition(BaseModel):
    """执行前必须满足的前置条件；不满足则 Planner 插入前置采样步骤或放弃。"""

    kind: Literal["tool_returns", "metric_threshold", "custom"] = "tool_returns"
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    assert_expr: str = Field(
        description="断言表达式，例如 'response.status == \"ok\"' 或 'result.value > 0'",
    )
    description: str = ""


class SkillStep(BaseModel):
    """Skill 计划模板中的单步。"""

    id: str = Field(description="步骤在 Skill 内的稳定 id（供 depends_on / verifications 引用）")
    description: str
    tool_hint: str | None = None
    tool_args_template: dict[str, Any] = Field(
        default_factory=dict,
        description="工具参数模板，支持 '{{ context.instance }}' 占位；Executor 层渲染",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="本步骤依赖的其他 Skill 步骤 id",
    )
    parallel: bool = True
    optional: bool = False
    expected_output: str | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, value: Any) -> Any:
        if value is None:
            return value
        return str(value).strip()


class SkillVerification(BaseModel):
    """执行完毕后自动跑的验收；失败则 Replanner 强制继续补步骤。"""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    success_expr: str = Field(description="成功判定表达式，例如 'status in (\"green\", \"yellow\")'")
    description: str = ""


class SkillPack(BaseModel):
    """Skill Pack：触发 → 前置 → 计划 → 验收 → 后置修复/回滚 的自包含单元。"""

    model_config = ConfigDict(extra="ignore")

    skill_id: str
    version: str = "1.0.0"
    domain: str = "aiops"
    title: str
    description: str = ""
    trigger: SkillTrigger
    required_tools: list[str] = Field(
        default_factory=list,
        description="tool_registry 中必须存在的工具集合；缺失时 Skill 会被标记 disabled_reason",
    )
    required_role: Literal["viewer", "operator", "admin"] = "viewer"
    preconditions: list[SkillPrecondition] = Field(default_factory=list)
    steps: list[SkillStep]
    verifications: list[SkillVerification] = Field(default_factory=list)
    remediation: list[SkillStep] = Field(
        default_factory=list,
        description="修复动作步骤；risk_level>0 时必须走 action_governor",
    )
    rollback: list[SkillStep] = Field(default_factory=list)
    runbook_refs: list[str] = Field(
        default_factory=list,
        description="关联的 wiki runbook 文档 id 列表（aiops-docs/<doc_id>.md）；"
        "Executor 每步会按 step 描述从这些文档中抽取最相关的 h2 小节作为 LLM 上下文",
    )
    tags: list[str] = Field(default_factory=list)
    review_status: Literal["demo", "approved", "deprecated"] = "demo"
    source: str = Field(
        default="",
        description="加载来源标签（file:xxx.yaml / builtin-demo / legacy-playbook），用于诚实标注",
    )
    disabled_reason: str | None = Field(
        default=None,
        description="加载器校验失败时的原因（如 required_tools 缺失）；非空表示 Skill 不参与匹配",
    )

    @field_validator("steps")
    @classmethod
    def _ensure_step_ids_unique(cls, value: list[SkillStep]) -> list[SkillStep]:
        seen: set[str] = set()
        for step in value:
            if step.id in seen:
                raise ValueError(f"SkillStep id 重复: {step.id}")
            seen.add(step.id)
        return value

    def is_enabled(self) -> bool:
        """Registry 匹配时的开关：disabled_reason 非空则跳过该 Skill。"""
        return not self.disabled_reason

    def to_plan_step_dicts(self, id_offset: int = 0) -> list[dict[str, Any]]:
        """把 Skill.steps 转成 Planner state 层 dict。"""
        step_ids = {step.id for step in self.steps}
        result: list[dict[str, Any]] = []
        for step in self.steps:
            deps = [f"sk:{dep}" for dep in step.depends_on if dep in step_ids]
            result.append(
                {
                    "step_id": f"sk:{step.id}",
                    "description": step.description,
                    "tool_hint": step.tool_hint,
                    "expected_output": step.expected_output,
                    "depends_on": deps,
                    "skill_id": self.skill_id,
                    "skill_step_id": step.id,
                    "tool_args_template": dict(step.tool_args_template),
                    "optional": step.optional,
                }
            )
        return result
