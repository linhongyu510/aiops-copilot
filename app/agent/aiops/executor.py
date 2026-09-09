"""
Executor 节点：按依赖 DAG 分批执行计划步骤（P0.2 升级）
基于 LangGraph 官方教程实现

- 无依赖步骤按 aiops_max_parallel_steps 并行执行；
- 依赖步骤等其 step_id 进入 completed_steps 后才调度；
- 单步骤内部保持原有 LLM↔工具 多轮循环与并行 tool_calls 语义。
"""

import asyncio
import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from app.agent.aiops.models import (
    normalize_plan,
    ready_batch,
    step_description,
    step_id_of,
    step_task_text,
)
from app.agent.aiops.observations import compact_observation
from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.skills.context import use_skill_context
from app.agent.skills.renderer import build_render_context, render_args
from app.agent.skills.runbook_retriever import get_runbook_retriever
from app.agent.tool_router import select_relevant_tools
from app.config import config
from app.core.llm_factory import llm_factory
from app.tools import analyze_topology, get_current_time, retrieve_knowledge

from .state import PlanExecuteState


def _fetch_runbook_hint(skill_id: str, step_desc: str) -> str:
    """从 skill_id 对应 SkillPack 的 runbook_refs 里抽取最相关 h2 小节。

    返回值为多段 markdown（每段是 `【参考：doc / section】…`），若 skill 未声明
    runbook 或抽取为空则返回空串。放在这里而非 hot path 里 inline，方便测试打桩。
    """
    if not skill_id or not step_desc:
        return ""
    try:
        from app.agent.skills import get_skill_registry
    except Exception:
        return ""
    pack = get_skill_registry().get(skill_id)
    if pack is None or not pack.runbook_refs:
        return ""
    excerpts = get_runbook_retriever().extract_relevant(
        pack.runbook_refs, step_desc, top_k=2, max_chars_per_section=600
    )
    if not excerpts:
        return ""
    return "\n\n---\n\n".join(ex.as_prompt_block() for ex in excerpts)


async def _run_tool_call(tool_map: dict[str, Any], tool_call: dict[str, Any]) -> ToolMessage:
    """执行单个工具调用，失败转为错误文本结果（不向外抛异常）"""
    tool_name = tool_call.get("name", "")
    tool = tool_map.get(tool_name)
    if tool is None:
        tool_result = f"工具 {tool_name!r} 不存在或当前不可用"
    else:
        try:
            tool_result = await tool.ainvoke(tool_call.get("args", {}))
        except Exception as tool_exc:
            logger.warning(f"工具 {tool_name} 执行失败: {tool_exc}")
            tool_result = f"工具 {tool_name} 执行失败: {tool_exc}"

    if isinstance(tool_result, tuple):
        tool_result = tool_result[0]
    return ToolMessage(
        content=str(tool_result),
        tool_call_id=tool_call.get("id", tool_name or "unknown"),
        name=tool_name or None,
    )


async def _execute_single_step(
    step: dict[str, Any],
    input_text: str,
    all_tools: list[Any],
    render_context: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """执行单个步骤：LLM↔工具 多轮循环，返回 (描述, 结果)。"""

    task = step_task_text(step)
    logger.info(f"当前任务: {step_description(step)}")

    # P1-3：把当前步骤的 Skill 归因写入 asyncio contextvar，
    # 供 action_governor.evaluate 在 MCP 咽喉处读取并塞入 ActionProposal。
    # step 未标注（LLM 兜底生成的 plan）时字段留空，走向后兼容路径。
    step_skill_id = str(step.get("skill_id") or "") if isinstance(step, dict) else ""
    step_skill_step_id = (
        str(step.get("skill_step_id") or "") if isinstance(step, dict) else ""
    )

    # Skill 步骤：把 tool_args_template 渲染后作为「建议参数」附加到任务描述，
    # LLM 会优先使用；模板缺占位时保留原文，Executor 的 LLM 兜底仍可自行判断。
    template = step.get("tool_args_template") if isinstance(step, dict) else None
    if template and render_context is not None:
        rendered = render_args(template, render_context)
        if isinstance(rendered, dict) and rendered:
            try:
                args_json = json.dumps(rendered, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                args_json = str(rendered)
            task = f"{task}\n（Skill 建议参数：{args_json}，如与实际工具签名不符请自行调整）"

    # 从 Skill 声明的 runbook_refs 里按当前步骤描述定向抽取 wiki 小节，
    # 作为 LLM 上下文。不依赖 Milvus，直接读 aiops-docs/*.md。
    # step 未归属 Skill 或 Skill 无 runbook_refs 时跳过。
    runbook_hint = _fetch_runbook_hint(step_skill_id, step_description(step))
    if runbook_hint:
        task = f"{task}\n\n参考 Runbook 段落（供决策，不要照抄）：\n{runbook_hint}"

    # 按当前步骤 + 原始输入裁剪工具，减少每轮 LLM 调用的工具描述开销。
    # select_relevant_tools 内部有兜底（DEFAULT_TOOL_PREFIXES / tools[:limit]），保证结果非空
    exposed_tools = select_relevant_tools(
        f"{step_description(step)} {input_text}",
        all_tools,
        limit=config.aiops_max_exposed_tools,
    )
    logger.info(f"本步骤暴露工具数量: {len(exposed_tools)} / {len(all_tools)}")

    # 创建 LLM（绑定工具）
    llm = llm_factory.create_chat_model(
        model=config.rag_model,
        temperature=0,
        streaming=False,
        max_tokens=2048,
    )
    llm_with_tools = llm.bind_tools(exposed_tools)

    # 显式执行工具，避免依赖 ToolNode 在不同 LangGraph 版本中的内部配置协议。
    tool_map = {tool.name: tool for tool in exposed_tools}

    # 构建消息（只包含当前步骤，避免原始任务干扰）
    messages = [
        SystemMessage(
            content="""你是一个能力强大的助手，负责执行具体的任务步骤。

你可以使用各种工具来完成任务。对于每个步骤：
1. 理解步骤的目标
2. 选择合适的工具，如果已经指定了工具，则使用指定的工具
3. 调用工具获取信息；如果一个工具的输出是另一个工具的输入（例如先查 topic_id 再查日志），请分多轮依次调用
4. 返回执行结果

注意：
- 如果工具调用失败，请说明失败原因
- 不要编造数据，只返回实际获取的信息
- 执行结果要清晰、准确
- 专注于当前步骤，不要考虑其他任务"""
        ),
        HumanMessage(content=f"请执行以下任务: {task}"),
    ]

    # 循环执行：LLM -> 若有 tool_calls 则执行并回灌 -> 再 LLM
    # 直到 LLM 不再请求工具调用，或达到最大迭代轮数
    max_iterations = config.agent_step_max_iterations
    result = ""

    with use_skill_context(
        skill_id=step_skill_id, skill_step_id=step_skill_step_id
    ):
        for iteration in range(1, max_iterations + 1):
            llm_response = await llm_with_tools.ainvoke(messages)
            tool_calls = getattr(llm_response, "tool_calls", None)

            if not tool_calls:
                # LLM 不再请求工具调用，当前输出即为步骤结果
                logger.info(f"第 {iteration} 轮：LLM 未请求工具调用，步骤执行结束")
                result = (
                    llm_response.content
                    if hasattr(llm_response, "content")
                    else str(llm_response)
                )
                break

            logger.info(f"第 {iteration} 轮：检测到 {len(tool_calls)} 个工具调用，执行并回灌结果")
            messages.append(llm_response)
            if len(tool_calls) == 1:
                tool_messages = [await _run_tool_call(tool_map, tool_calls[0])]
            else:
                # 同一轮内多个 tool_calls 语义独立，并行执行；
                # gather 保持结果顺序，与 tool_calls 顺序一致
                tool_messages = await asyncio.gather(
                    *(_run_tool_call(tool_map, tc) for tc in tool_calls)
                )
            messages.extend(tool_messages)
        else:
            # 达到迭代上限：不再绑定工具，强制 LLM 基于已收集的工具结果生成步骤总结
            logger.warning(
                f"工具调用达到最大迭代轮数 {max_iterations}，"
                "不再绑定工具，基于已有工具结果生成步骤总结"
            )
            final_response = await llm.ainvoke(messages)
            result = (
                final_response.content
                if hasattr(final_response, "content")
                else str(final_response)
            )

    if not isinstance(result, str):
        result = str(result)

    logger.info(f"步骤执行完成，结果长度: {len(result)}")
    return step_description(step), result


async def executor(state: PlanExecuteState) -> dict[str, Any]:
    """
    执行节点：按依赖 DAG 选出一批就绪步骤并行执行
    """
    logger.info("=== Executor：执行步骤 ===")

    plan = normalize_plan(state.get("plan", []))

    # 如果计划为空，不执行
    if not plan:
        logger.info("计划为空，跳过执行")
        return {}

    completed_ids = set(state.get("completed_steps", []))
    batch = ready_batch(plan, completed_ids, config.aiops_max_parallel_steps)
    batch_ids = {step["step_id"] for step in batch}
    remaining = [step for step in plan if step["step_id"] not in batch_ids]
    logger.info(
        f"就绪步骤 {len(batch)}/{len(plan)}（并行上限 "
        f"{config.aiops_max_parallel_steps}），剩余 {len(remaining)} 个待后续批次"
    )

    # 获取本地工具
    local_tools = [get_current_time, retrieve_knowledge, analyze_topology]

    # 获取 MCP 工具（失败时本批步骤全部记为执行失败，不向外抛异常）
    try:
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")
        all_tools = local_tools + mcp_tools
    except Exception as exc:
        logger.error(f"获取 MCP 工具失败: {exc}", exc_info=True)
        return {
            "plan": remaining,
            "past_steps": [
                (step_description(step), f"执行失败: {str(exc)}") for step in batch
            ],
            "completed_steps": [step["step_id"] for step in batch],
        }

    input_text = state.get("input", "")

    # 构造 Skill 参数渲染 context：input + skill_context + labels + 已完成
    # 步骤的 skill_step_id → 结果映射。步骤未标注 skill_step_id 时不影响。
    past_steps = state.get("past_steps", []) or []
    step_id_to_result: dict[str, str] = {}
    for description, result in past_steps:
        # past_steps 是 (description, compact_result) 元组；跨步引用只能靠
        # description。若上游想按 skill_step_id 引用，需在 planner 阶段把
        # description 打成可识别 key，或后续再引入结构化 artifacts。
        step_id_to_result[description] = str(result)
    skill_context_state = state.get("skill_context") or {}
    render_context = build_render_context(
        input_text=input_text,
        past_steps=past_steps,
        step_id_to_result=step_id_to_result,
        skill_context=skill_context_state,
        labels=skill_context_state.get("labels") if isinstance(skill_context_state, dict) else None,
    )

    async def _safe_execute(step: dict[str, Any]) -> tuple[str, str, str]:
        step_id = step_id_of(step, fallback=step_description(step))
        try:
            description, result = await _execute_single_step(
                step, input_text, all_tools, render_context=render_context
            )
            return step_id, description, result
        except Exception as exc:
            logger.error(f"执行步骤失败: {exc}", exc_info=True)
            return step_id, step_description(step), f"执行失败: {str(exc)}"

    outcomes = await asyncio.gather(*(_safe_execute(step) for step in batch))

    # past_steps 放压缩视图（供 replanner 决策），artifacts 存完整原文
    #（供最终报告还原证据链）；两者都设上限防止长日志撑爆上下文
    past_steps = [
        (description, compact_observation(result, config.aiops_observation_max_chars))
        for _sid, description, result in outcomes
    ]
    artifacts = {
        description: compact_observation(result, config.aiops_artifact_max_chars)
        for _sid, description, result in outcomes
    }

    return {
        "plan": remaining,  # 移除本批已执行步骤
        "past_steps": past_steps,  # 使用 operator.add 追加
        "artifacts": artifacts,  # 使用 operator.or_ 合并
        "completed_steps": [step_id for step_id, _d, _r in outcomes],
    }
