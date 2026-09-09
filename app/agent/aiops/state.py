"""
通用 Plan-Execute-Replan 状态定义
基于 LangGraph 官方教程实现
"""

import operator
from typing import Annotated, TypedDict


class PlanExecuteState(TypedDict):
    """Plan-Execute-Replan 状态"""

    # 用户输入（任务描述）
    input: str

    # 执行计划：结构化步骤 dict 列表（step_id/description/tool_hint/
    # expected_output/depends_on，见 models.normalize_plan）；
    # 兼容旧格式 list[str]（等价于无依赖步骤）
    plan: list

    # 已完成的步骤 id（如 "s1"），供 Executor 做依赖就绪判定
    completed_steps: Annotated[list[str], operator.add]

    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    # 把operator.add 作为元数据
    past_steps: Annotated[list[tuple], operator.add]

    # 已执行步骤的完整原始输出存档（description → full result），
    # 最终报告优先引用存档以还原完整证据链；past_steps 只放压缩视图
    artifacts: Annotated[dict[str, str], operator.or_]

    # 计划是否为降级计划（Planner LLM 调用失败时使用默认计划）
    degraded: bool

    # 命中的 Skill 上下文（P0-1）：Planner 命中时写入，供 Executor 参数模板
    # 渲染、Replanner 验收闭环、ActionGovernor 提案回放共同引用
    skill_id: str
    skill_context: dict

    # 最终响应/报告
    response: str

    # 结构化诊断结果（P1-2 C3）：markdown 报告的机读伴生对象，供下游
    # incident_memory / 通知渠道 / SkillRegistry 度量复用（未启用时为空 dict）
    diagnosis_outcome: dict
