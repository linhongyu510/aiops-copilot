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

from app.agent.aiops.models import PlanStep, normalize_plan
from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.tool_router import select_relevant_tools
from app.config import config
from app.core.llm_factory import llm_factory, structured_output
from app.services.incident_memory_service import incident_memory_service
from app.tools import analyze_topology, get_current_time, retrieve_knowledge

from .state import PlanExecuteState
from .utils import format_tools_description

# Planner LLM 失败时使用的默认降级计划
DEFAULT_PLAN = ["收集相关信息", "分析数据", "生成报告"]


class Plan(BaseModel):
    """计划的输出格式"""

    steps: list[PlanStep] = Field(
        description="完成任务所需的步骤列表。每步包含描述、可选的工具提示与依赖，"
        "相互独立的步骤依赖填空列表，它们会被并行执行"
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
                - 将任务分解为逻辑上独立的步骤，每一步建立在前面的基础上
                - 每个步骤应该明确使用哪些工具(如果需要工具的话)来获取信息, 最好能同时提供工具执行所需要的参数
                - **相互独立的步骤（彼此不需要对方的输出）必须把 depends_on 填为空列表**，这些步骤会被并行执行，显著缩短排查时间
                - 只有确实需要等待另一步输出时才在 depends_on 中填写该步骤的序号（1-based：依赖"步骤1"填 1）
                - 步骤描述要具体、可操作
                - **如果有相关经验文档，请参考其中的方法和步骤制定计划**

                示例输入："分析当前系统的性能问题"
                示例输出（假设有对应工具）：
                步骤1: 使用 get_metrics 工具收集系统的 CPU 和内存使用情况 (depends_on: [])
                步骤2: 使用 query_logs 工具检查最近的错误日志 (depends_on: [])
                步骤3: 使用 query_database 工具分析慢查询日志，关联步骤1/2 的异常时段 (depends_on: [1, 2])
                步骤4: 综合以上信息生成性能分析报告 (depends_on: [1, 2, 3])
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
        local_tools = [get_current_time, retrieve_knowledge, analyze_topology]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()

        # 合并所有工具
        all_tools = local_tools + mcp_tools
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 按用户输入裁剪工具，只把相关工具描述注入规划 prompt
        exposed_tools = select_relevant_tools(
            input_text, all_tools, limit=config.aiops_max_exposed_tools
        )
        logger.info(f"规划注入工具描述数量: {len(exposed_tools)} / {len(all_tools)}")

        # 格式化工具描述
        tools_description = format_tools_description(exposed_tools)

        # 步骤3: 组装经验上下文 = runbook 检索 + 历史相似事件（P0.3）+ 匹配预案（P2.4）
        context_sections: list[str] = []
        if experience_docs:
            context_sections.append(
                dedent(f"""
                    ## 相关经验文档

                    以下是从知识库中检索到的相关经验和最佳实践，请参考这些经验制定执行计划：

                    {experience_docs}
                """).strip()
            )
        try:
            recalled_episodes = incident_memory_service.format_recalled_episodes(input_text)
            if recalled_episodes:
                context_sections.append(recalled_episodes)
                logger.info("事件记忆命中相似历史事件，已注入规划上下文")
        except Exception as e:
            logger.warning(f"检索历史相似事件失败（跳过，不影响规划）: {e}")

        # Skill Registry（P0-1）：优先匹配可执行 Skill；命中时后续会跳过 LLM 拆解
        top_skill_match = None
        try:
            from app.agent.skills import get_skill_registry

            skill_registry = get_skill_registry()
            skill_matches = skill_registry.match(input_text)
            if skill_matches:
                top_skill_match = skill_matches[0]
                context_sections.append(
                    skill_registry.format_matched_skills(input_text)
                )
                logger.info(
                    f"Skill 命中：{top_skill_match.skill.skill_id} "
                    f"(score={top_skill_match.score}, reasons={top_skill_match.reasons})"
                )
        except Exception as e:
            logger.warning(f"Skill 匹配失败（跳过，不影响规划）: {e}")

        try:
            from app.services.playbook_service import get_playbook_service

            matched_playbooks = get_playbook_service().format_matched_playbooks(input_text)
            if matched_playbooks:
                context_sections.append(matched_playbooks)
                logger.info("预案库命中匹配预案，已注入规划上下文")
        except Exception as e:
            logger.warning(f"检索匹配预案失败（跳过，不影响规划）: {e}")
        experience_context = "\n\n---\n\n".join(context_sections)

        # 命中 Skill：直接把 skill.steps 作为结构化 plan 返回，跳过 LLM 拆解
        if top_skill_match is not None:
            skill = top_skill_match.skill
            skill_plan = normalize_plan(skill.to_plan_step_dicts())
            if skill_plan:
                logger.info(
                    f"计划直接采用 Skill '{skill.skill_id}'，共 {len(skill_plan)} 个步骤"
                )
                try:
                    from app.agent.skills import get_skill_registry as _reg
                    _reg().record_activation(skill.skill_id)
                except Exception:
                    pass
                return {
                    "plan": skill_plan,
                    "degraded": False,
                    "skill_id": skill.skill_id,
                    "skill_context": {
                        "skill_id": skill.skill_id,
                        "match_score": top_skill_match.score,
                        "match_reasons": list(top_skill_match.reasons),
                        "verifications": [
                            v.model_dump() for v in skill.verifications
                        ],
                        "required_role": skill.required_role,
                    },
                }

        # 步骤4: 创建 LLM 并生成计划
        llm = llm_factory.create_chat_model(
            model=config.rag_model,
            temperature=0,
            streaming=False,
            max_tokens=1024,
        )

        planner_chain = planner_prompt | structured_output(llm, Plan)

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

        # 归一化为结构化步骤（稳定 step_id + 依赖翻译），空计划视为失败
        plan_steps = normalize_plan(plan_steps)
        if not plan_steps:
            raise RuntimeError("LLM 返回的计划步骤为空")

        logger.info(f"计划已生成，共 {len(plan_steps)} 个步骤")
        for step in plan_steps:
            deps = step.get("depends_on") or []
            logger.info(
                f"  步骤{step['step_id']}: {step['description']}"
                + (f" (依赖: {', '.join(deps)})" if deps else " (可并行)")
            )

        return {"plan": plan_steps, "degraded": False}

    except Exception as e:
        # 重试后仍失败：返回默认计划，并显式标记 degraded，便于调用方/前端感知
        logger.warning(f"生成计划失败（重试后仍失败），使用默认降级计划: {e}", exc_info=True)
        return {"plan": normalize_plan(DEFAULT_PLAN), "degraded": True}


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
