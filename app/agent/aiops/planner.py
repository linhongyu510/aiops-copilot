"""
Planner 节点：制定执行计划
基于 LangGraph 官方教程实现
"""

import re
from textwrap import dedent
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.core.llm_factory import llm_factory
from app.tools import get_current_time, retrieve_knowledge

from .state import PlanExecuteState
from .utils import format_tools_description

# Planner LLM 失败时使用的默认降级计划
DEFAULT_PLAN = ["收集相关信息", "分析数据", "生成报告"]


class Plan(BaseModel):
    """计划的输出格式"""

    steps: list[str] = Field(
        description="完成任务所需的不同步骤。这些步骤应该按顺序执行，每一步都建立在前一步的基础上。"
    )


# Planner 提示词
planner_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            dedent("""
                作为一个专家级别的规划者，你需要将复杂的任务分解为可执行的步骤。

                可用工具列表（用于制定计划时参考）：

                {tools_description}

                注意：你的职责是制定计划，实际的工具调用由 Executor 负责执行。

                {experience_context}

                对于给定的任务，请创建一个简单的、逐步的计划来完成它。计划应该：
                - 将任务分解为逻辑上独立的步骤
                - 每个步骤应该明确使用哪些工具(如果需要工具的话)来获取信息, 最好能同时提供工具执行所需要的参数
                - 步骤之间应该有清晰的依赖关系
                - 步骤描述要具体、可操作
                - **如果有相关经验文档，请参考其中的方法和步骤制定计划**

                示例输入："分析当前系统的性能问题"
                示例输出（假设有对应工具）：
                步骤1: 使用 get_metrics 工具收集系统的 CPU 和内存使用情况
                步骤2: 使用 query_logs 工具检查最近的错误日志
                步骤3: 使用 query_database 工具分析慢查询日志
                步骤4: 综合以上信息生成性能分析报告
            """).strip(),
        ),
        ("placeholder", "{messages}"),
    ]
)


async def planner(state: PlanExecuteState) -> dict[str, Any]:
    """
    规划节点：根据用户输入生成执行计划

    流程：
    1. 先查询内部文档，获取相关经验和最佳实践
    2. 基于经验文档和可用工具制定执行计划
    """
    logger.info("=== Planner：制定执行计划 ===")

    input_text = state.get("input", "")
    logger.info(f"用户输入: {input_text}")

    try:
        # 步骤1: 查询内部文档获取相关经验
        logger.info("查询内部文档，寻找相关经验...")
        experience_docs = ""
        try:
            # retrieve_knowledge 使用 response_format="content_and_artifact"
            # ainvoke() 只返回 content（字符串），不是元组
            context_str = await retrieve_knowledge.ainvoke({"query": input_text})
            if context_str and context_str.strip():
                experience_docs = context_str
                logger.info(f"找到相关经验文档，长度: {len(experience_docs)}")
            else:
                logger.info("未找到相关经验文档")
        except Exception as e:
            logger.warning(f"查询内部文档失败: {e}")

        # 步骤2: 获取可用工具列表
        # 获取本地工具
        local_tools = [get_current_time, retrieve_knowledge]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 格式化工具描述
        tools_description = format_tools_description(all_tools)

        # 步骤3: 格式化经验文档上下文
        if experience_docs:
            experience_context = dedent(f"""
                ## 相关经验文档

                以下是从知识库中检索到的相关经验和最佳实践，请参考这些经验制定执行计划：

                {experience_docs}

                ---
            """).strip()
        else:
            experience_context = ""

        # 步骤4: 创建 LLM 并生成计划
        llm = llm_factory.create_chat_model(
            model=config.rag_model,
            temperature=0,
            streaming=False,
            max_tokens=1024,
        )

        planner_chain = planner_prompt | llm.with_structured_output(Plan)

        try:
            # 首选：structured output 方式生成计划
            plan_result = await planner_chain.ainvoke(
                {
                    "messages": [("user", input_text)],
                    "tools_description": tools_description,
                    "experience_context": experience_context,
                }
            )

            # 提取步骤列表
            if isinstance(plan_result, Plan):
                plan_steps = plan_result.steps
            else:
                # 如果返回的是字典，提取 steps 字段
                plan_steps = plan_result.get("steps", [])  # type: ignore
        except Exception as structured_exc:
            # 重试 1 次：关闭 structured output，使用纯文本兜底解析
            logger.warning(f"结构化计划生成失败，重试 1 次（纯文本解析）: {structured_exc}")
            plan_steps = await _plan_with_plain_text(
                llm, input_text, tools_description, experience_context
            )

        if not plan_steps:
            raise RuntimeError("LLM 返回的计划步骤为空")

        logger.info(f"计划已生成，共 {len(plan_steps)} 个步骤")
        for i, step in enumerate(plan_steps, 1):
            logger.info(f"  步骤{i}: {step}")

        return {"plan": plan_steps, "degraded": False}

    except Exception as e:
        # 重试后仍失败：返回默认计划，并显式标记 degraded，便于调用方/前端感知
        logger.warning(f"生成计划失败（重试后仍失败），使用默认降级计划: {e}", exc_info=True)
        return {"plan": DEFAULT_PLAN, "degraded": True}


async def _plan_with_plain_text(
    llm: Any,
    input_text: str,
    tools_description: str,
    experience_context: str,
) -> list[str]:
    """
    降级重试：不使用 structured output，直接请求纯文本计划并解析步骤列表

    用于结构化输出（如 function calling）不被模型支持或调用失败时的兜底。
    """
    prompt = planner_prompt.format_prompt(
        messages=[("user", input_text)],
        tools_description=tools_description,
        experience_context=experience_context,
    )
    response = await llm.ainvoke(prompt.to_messages())
    text = response.content if hasattr(response, "content") else str(response)
    if not isinstance(text, str):
        text = str(text)
    return _parse_plan_steps(text)


def _parse_plan_steps(text: str, max_steps: int = 10) -> list[str]:
    """
    从纯文本中解析计划步骤

    支持以下行格式：
    - "步骤1: xxx" / "步骤 1：xxx"
    - "1. xxx" / "1、xxx" / "1) xxx"
    - "- xxx" / "* xxx"
    """
    steps: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        cleaned = re.sub(
            r"^(?:步骤\s*\d+\s*[:：.，]?|\d+\s*[.、)）]|[-*•])\s*", "", line
        ).strip()
        if cleaned:
            steps.append(cleaned)
        if len(steps) >= max_steps:
            break
    return steps
