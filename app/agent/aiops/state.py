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

    # 执行计划（步骤列表）
    plan: list[str]

    # 已执行的步骤历史
    # 使用 operator.add 实现追加式更新（而非覆盖）
    # 把operator.add 作为元数据
    past_steps: Annotated[list[tuple], operator.add]

    # 计划是否为降级计划（Planner LLM 调用失败时使用默认计划）
    degraded: bool

    # 最终响应/报告
    response: str
