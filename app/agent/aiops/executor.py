"""
Executor 节点：执行单个步骤
基于 LangGraph 官方教程实现
"""

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry
from app.config import config
from app.core.llm_factory import llm_factory
from app.tools import get_current_time, retrieve_knowledge

from .state import PlanExecuteState


async def executor(state: PlanExecuteState) -> dict[str, Any]:
    """
    执行节点：执行计划中的下一个步骤

    使用 LangGraph 的 ToolNode 自动处理工具调用
    """
    logger.info("=== Executor：执行步骤 ===")

    plan = state.get("plan", [])

    # 如果计划为空，不执行
    if not plan:
        logger.info("计划为空，跳过执行")
        return {}

    # 取出第一个步骤
    task = plan[0]
    logger.info(f"当前任务: {task}")

    try:
        # 获取本地工具
        local_tools = [get_current_time, retrieve_knowledge]

        # 获取 MCP 工具
        mcp_client = await get_mcp_client_with_retry()
        mcp_tools = await mcp_client.get_tools()
        logger.info(f"可用工具数量: 本地 {len(local_tools)} + MCP {len(mcp_tools)}")

        # 合并所有工具
        all_tools = local_tools + mcp_tools

        # 创建 LLM（绑定工具）
        llm = llm_factory.create_chat_model(
            model=config.rag_model,
            temperature=0,
            streaming=False,
            max_tokens=2048,
        )
        llm_with_tools = llm.bind_tools(all_tools)

        # 显式执行工具，避免依赖 ToolNode 在不同 LangGraph 版本中的内部配置协议。
        tool_map = {tool.name: tool for tool in all_tools}

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

        # 循环执行：LLM -> 若有 tool_calls 则 ToolNode 执行并回灌 -> 再 LLM
        # 直到 LLM 不再请求工具调用，或达到最大迭代轮数
        max_iterations = config.agent_step_max_iterations
        result = ""

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
            for tool_call in tool_calls:
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
                messages.append(
                    ToolMessage(
                        content=str(tool_result),
                        tool_call_id=tool_call.get("id", tool_name or "unknown"),
                        name=tool_name or None,
                    )
                )
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

        # 返回更新：移除已执行的步骤，添加执行历史
        return {
            "plan": plan[1:],  # 移除第一个步骤
            "past_steps": [(task, result)],  # 使用 operator.add 追加
        }

    except Exception as e:
        logger.error(f"执行步骤失败: {e}", exc_info=True)
        return {
            "plan": plan[1:],
            "past_steps": [(task, f"执行失败: {str(e)}")],
        }
