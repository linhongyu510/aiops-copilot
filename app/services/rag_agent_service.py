"""RAG Agent 服务 - 基于 LangGraph 的智能代理

模型通过 llm_factory 创建 OpenAI 兼容客户端（Ollama / DashScope / DeepSeek），
支持真正的流式输出和更好的模型适配。
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from typing import Annotated, Any

from langchain.agents import create_agent
from langchain.agents.middleware import before_model
from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.runtime import Runtime
from loguru import logger
from typing_extensions import TypedDict

from app.agent.mcp_client import get_mcp_client_with_retry
from app.agent.tool_registry import tool_registry
from app.agent.tool_router import dynamic_tool_router
from app.config import config
from app.core.llm_factory import llm_factory
from app.tools import get_current_time, retrieve_knowledge

# 阿里千问大模型和langchain集成参考： https://docs.langchain.com/oss/python/integrations/chat/qwen
# 注意：需要配置环境变量 DASHSCOPE_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1 否则默认访问的是新加坡站点
# 同时也需要配置环境变量 DASHSCOPE_API_KEY=your_api_key


class AgentState(TypedDict):
    """Agent 状态"""

    messages: Annotated[Sequence[BaseMessage], add_messages]


@before_model
def trim_messages_middleware(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """
    修剪消息历史，只保留最近的几条消息以适应上下文窗口

    策略：
    - 保留第一条系统消息（System Message）
    - 保留最近的 6 条消息（3 轮对话）
    - 当消息少于等于 7 条时，不做修剪

    Args:
        state: Agent 状态
        runtime: LangGraph 运行时上下文（本中间件未使用）

    Returns:
        包含修剪后消息的字典，如果无需修剪则返回 None
    """
    messages = state["messages"]

    # 如果消息数量较少，无需修剪
    if len(messages) <= 7:
        return None

    # 提取第一条系统消息
    first_msg = messages[0]

    # 保留最近的 6 条消息（确保包含完整的对话轮次）
    recent_messages = messages[-6:] if len(messages) % 2 == 0 else messages[-7:]

    # 构建新的消息列表
    new_messages = [first_msg] + list(recent_messages)

    logger.debug(f"修剪消息历史: {len(messages)} -> {len(new_messages)} 条")

    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *new_messages]}


class RagAgentService:
    """RAG Agent 服务 - 使用 LangGraph + ChatQwen 原生集成"""

    def __init__(self, streaming: bool = True):
        """初始化 RAG Agent 服务

        Args:
            streaming: 是否启用流式输出，默认为 True
        """
        self.model_name = config.rag_model
        self.streaming = streaming
        self.system_prompt = self._build_system_prompt()

        self.model = llm_factory.create_chat_model(
            model=self.model_name,
            temperature=0.7,
            streaming=streaming,
        )

        # 定义基础工具
        self.tools = [retrieve_knowledge, get_current_time]

        # MCP 客户端（延迟初始化，使用全局管理）
        self.mcp_tools: list = []

        # 创建内存检查点（用于会话管理）
        self.checkpointer = MemorySaver()

        # Agent 初始化（会在异步方法中完成）
        self.agent = None
        self._agent_initialized = False
        self._agent_init_lock = asyncio.Lock()

        logger.info(
            f"RAG Agent 服务初始化完成 ({config.llm_provider}), "
            f"model={self.model_name}, streaming={streaming}"
        )

    def configure_checkpointer(self, checkpointer: Any) -> None:
        """Install a lifecycle-owned saver before the agent graph is compiled."""
        if self._agent_initialized:
            raise RuntimeError("cannot replace checkpointer after agent initialization")
        self.checkpointer = checkpointer

    async def _initialize_agent(self):
        """异步初始化 Agent（包括 MCP 工具）"""
        if self._agent_initialized:
            return
        async with self._agent_init_lock:
            if self._agent_initialized:
                return

            # MCP 不可用时保留本地知识检索和时间工具，避免整个问答服务失效。
            try:
                mcp_client = await get_mcp_client_with_retry()
                self.mcp_tools = await mcp_client.get_tools()
                logger.info(f"成功加载 {len(self.mcp_tools)} 个 MCP 工具")
            except Exception as exc:
                self.mcp_tools = []
                logger.warning(f"MCP 工具加载失败，将以本地工具降级运行: {exc}")

            all_tools = self.tools + self.mcp_tools
            self.agent = create_agent(
                self.model,
                tools=all_tools,
                # 通过框架的 prompt 机制注入系统提示词：
                # system_prompt 在每次模型调用时动态拼入请求，不会写入 checkpoint，
                # 避免同一 thread 每轮对话重复累积 SystemMessage
                system_prompt=self.system_prompt,
                # 挂载消息修剪中间件，防止长会话超出上下文窗口
                middleware=[trim_messages_middleware, dynamic_tool_router],
                checkpointer=self.checkpointer,
            )
            self._agent_initialized = True

            tool_names = [tool.name if hasattr(tool, "name") else str(tool) for tool in all_tools]
            logger.info(f"可用工具列表: {', '.join(tool_names)}")

    def _build_system_prompt(self) -> str:
        """
        构建系统提示词

        注意：LangChain 框架会自动将工具信息传递给 LLM，
        因此系统提示词中无需列举具体的工具列表。

        Returns:
            str: 系统提示词
        """
        from textwrap import dedent

        return dedent("""
            你是一个专业的AI助手，能够使用多种工具来帮助用户解决问题。

            工作原则:
            1. 理解用户需求，选择合适的工具来完成任务
            2. 当需要获取实时信息或专业知识时，主动使用相关工具
            3. 基于工具返回的结果提供准确、专业的回答
            4. 如果工具无法提供足够信息，请诚实地告知用户
            5. 用户明确要求“基于内部运维知识库”时，只调用 retrieve_knowledge，
               不调用联网搜索，也不要混入工具返回内容之外的产品或厂商信息

            回答要求:
            - 保持友好、专业的语气
            - 回答简洁明了，重点突出
            - 运维问答控制在 200 字以内，优先给出检查顺序、证据和止损动作
            - 基于事实，不编造信息
            - 如有不确定的地方，明确说明

            请根据用户的问题，灵活使用可用工具，提供高质量的帮助。
        """).strip()

    @staticmethod
    def _format_retrieval_fallback(context: Any) -> str:
        """在模型不可用时返回可核验的原始检索证据。"""
        if isinstance(context, tuple):
            context = context[0]
        text = str(context).strip()
        if not text or text == "没有找到相关信息。" or text.startswith("检索知识时发生错误"):
            raise RuntimeError(text or "知识库未返回内容")
        return "模型服务暂不可用，以下为知识库直接检索结果，尚未经过 LLM 归纳：\n\n" + text[:4000]

    async def _retrieval_fallback(self, question: str) -> str:
        context = await self._retrieve_internal_knowledge(question)
        return self._format_retrieval_fallback(context)

    async def _retrieve_internal_knowledge(self, question: str) -> Any:
        """隔离 StructuredTool，便于替换检索实现和编写稳定单测。"""
        return await retrieve_knowledge.ainvoke({"query": question})

    @staticmethod
    def _is_internal_knowledge_query(question: str) -> bool:
        """判断是否为显式内部知识库请求（确定性路由条件，query/query_stream 共用）"""
        return "内部运维知识库" in question

    async def _query_internal_knowledge(self, question: str) -> str:
        """
        内部运维知识库确定性路由：一次检索、一次生成（query/query_stream 共用）

        只调用 retrieve_knowledge，不调用联网搜索，不进入完整 ReAct Agent。
        """
        context = await self._retrieve_internal_knowledge(question)
        messages = [
            SystemMessage(
                content=(
                    "你是 OnCall 助手。只根据给定内部知识回答，控制在 300 字以内；"
                    "每个事实使用资料编号 [1] 至 [5] 引证。给出排查顺序、证据或止损动作；"
                    "证据不足时明确说明，不得补充外部厂商信息或编造结论。"
                )
            ),
            HumanMessage(content=f"问题：{question}\n\n内部知识：\n{context}"),
        ]
        try:
            response = await self._invoke_summary_with_retry(messages)
            return str(response.content)
        except Exception as exc:
            logger.warning(f"模型调用失败，返回知识库检索降级结果: {exc}")
            return self._format_retrieval_fallback(context)

    async def _invoke_summary_with_retry(self, messages: list[Any]) -> Any:
        """Retry one transient model failure before returning retrieval-only evidence."""
        attempts = 2
        for attempt in range(1, attempts + 1):
            try:
                return await self.model.ainvoke(messages)
            except Exception as exc:
                error_name = type(exc).__name__.lower()
                message = str(exc).lower()
                transient = any(
                    marker in error_name or marker in message
                    for marker in ("connection", "timeout", "rate", "temporarily", "503")
                )
                if not transient or attempt == attempts:
                    raise
                wait_seconds = 0.5 * attempt
                logger.warning(
                    f"模型摘要调用发生瞬时错误，第 {attempt}/{attempts} 次失败，"
                    f"{wait_seconds:.1f}s 后重试: {type(exc).__name__}"
                )
                await asyncio.sleep(wait_seconds)
        raise RuntimeError("模型摘要重试状态异常")

    async def _explicit_read_only_tool_fallback(self, question: str) -> str | None:
        """Execute an explicitly named safe tool when the model router is unavailable.

        Eligibility comes from the tool registry (``read_only`` and ``risk_level``),
        not from a hardcoded vendor list, so any integration — first-party or
        third-party — participates as soon as it registers itself as read-only.
        """
        requested = next(
            (
                tool.name
                for tool in self.mcp_tools
                if tool.name
                and tool.name in question
                and tool_registry.spec_for(tool.name).read_only
                and tool_registry.spec_for(tool.name).risk_level == 0
            ),
            None,
        )
        if requested is None:
            return None
        tool = next(
            (candidate for candidate in self.mcp_tools if candidate.name == requested), None
        )
        if tool is None:
            return None
        result = await tool.ainvoke({})
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False, default=str)
        return (
            f"模型服务暂时不可用；已按请求直接执行只读工具 {requested}。"
            "以下为实时工具证据，未经过 LLM 归纳：\n\n"
            f"{result[:8000]}"
        )

    async def query(
        self,
        question: str,
        session_id: str,
    ) -> str:
        """
        非流式处理用户问题（一次性返回完整答案）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Returns:
            str: 完整答案
        """
        try:
            await self._initialize_agent()

            logger.info(f"[会话 {session_id}] RAG Agent 收到查询（非流式）: {question}")

            # 对显式内部知识库请求使用确定性策略路由：一次检索、一次生成。
            # 通用请求仍进入完整 ReAct Agent，并保留所有 MCP 工具。
            if self._is_internal_knowledge_query(question):
                return await self._query_internal_knowledge(question)

            # 构建消息列表（仅用户问题；系统提示词由 create_agent 的 system_prompt 注入，
            # 不再写入 checkpoint，避免同一 thread 每轮重复累积 SystemMessage）
            messages = [HumanMessage(content=question)]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            # 配置 thread_id（用于会话持久化）；recursion_limit 限制图的最大步数
            config_dict = {
                "configurable": {"thread_id": session_id},
                "recursion_limit": config.chat_recursion_limit,
            }

            result = await self.agent.ainvoke(
                input=agent_input,
                config=config_dict,
            )

            # 提取最终答案
            messages_result = result.get("messages", [])
            if messages_result:
                last_message = messages_result[-1]
                answer = (
                    last_message.content if hasattr(last_message, "content") else str(last_message)
                )

                # 记录工具调用
                if hasattr(last_message, "tool_calls") and last_message.tool_calls:
                    tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
                    logger.info(f"[会话 {session_id}] Agent 调用了工具: {tool_names}")

                logger.info(f"[会话 {session_id}] RAG Agent 查询完成（非流式）")
                return answer

            logger.warning(f"[会话 {session_id}] Agent 返回结果为空")
            return ""

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（非流式）: {e}")
            try:
                tool_fallback = await self._explicit_read_only_tool_fallback(question)
                if tool_fallback is not None:
                    logger.warning(f"[会话 {session_id}] 已降级为显式只读工具直调")
                    return tool_fallback
                fallback = await self._retrieval_fallback(question)
                logger.warning(f"[会话 {session_id}] 已降级为知识库直接检索")
                return fallback
            except Exception as fallback_exc:
                raise e from fallback_exc

    async def query_stream(
        self,
        question: str,
        session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        流式处理用户问题（逐步返回答案片段）

        Args:
            question: 用户问题
            session_id: 会话ID（作为 thread_id）

        Yields:
            Dict[str, Any]: 包含流式数据的字典
                - type: "content" | "tool_call" | "complete" | "error"
                - data: 具体内容
        """
        try:
            await self._initialize_agent()

            logger.info(f"[会话 {session_id}] RAG Agent 收到查询（流式）: {question}")

            # 对显式内部知识库请求使用与非流式 query() 相同的确定性路由：
            # 不进入 ReAct Agent，产出等价的流式事件（content + complete）
            if self._is_internal_knowledge_query(question):
                answer = await self._query_internal_knowledge(question)
                yield {"type": "content", "data": answer}
                yield {"type": "complete"}
                logger.info(f"[会话 {session_id}] 内部知识库路由查询完成（流式）")
                return

            # 构建消息列表（仅用户问题；系统提示词由 create_agent 的 system_prompt 注入，
            # 不再写入 checkpoint，避免同一 thread 每轮重复累积 SystemMessage）
            messages = [HumanMessage(content=question)]

            # 构建 Agent 输入
            agent_input = {"messages": messages}

            # 配置 thread_id（用于会话持久化）；recursion_limit 限制图的最大步数
            config_dict = {
                "configurable": {"thread_id": session_id},
                "recursion_limit": config.chat_recursion_limit,
            }

            async for token, metadata in self.agent.astream(
                input=agent_input,
                config=config_dict,
                stream_mode="messages",
            ):
                node_name = (
                    metadata.get("langgraph_node", "unknown")
                    if isinstance(metadata, dict)
                    else "unknown"
                )
                message_type = type(token).__name__

                if message_type in ("AIMessage", "AIMessageChunk"):
                    content_blocks = getattr(token, "content_blocks", None)

                    if content_blocks and isinstance(content_blocks, list):
                        for block in content_blocks:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_content = block.get("text", "")
                                if text_content:
                                    yield {
                                        "type": "content",
                                        "data": text_content,
                                        "node": node_name,
                                    }

            logger.info(f"[会话 {session_id}] RAG Agent 查询完成（流式）")
            yield {"type": "complete"}

        except Exception as e:
            logger.error(f"[会话 {session_id}] RAG Agent 查询失败（流式）: {e}")
            # 只 yield error 事件，不再 raise：api/chat.py 层捕获异常后会再 yield 一次 error，
            # raise 会导致客户端收到两条 error 事件
            yield {"type": "error", "data": str(e)}

    async def get_session_history(self, session_id: str) -> list:
        """
        获取会话历史（从 MemorySaver checkpointer 中读取）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            list: 消息历史列表 [{"role": "user|assistant", "content": "...", "timestamp": "..."}]
        """
        try:
            # 使用 checkpointer 的 get 方法获取最新的检查点
            config = {"configurable": {"thread_id": session_id}}

            # 获取该 thread 的最新检查点
            if hasattr(self.checkpointer, "aget_tuple"):
                checkpoint_tuple = await self.checkpointer.aget_tuple(config)
            else:
                checkpoint_tuple = self.checkpointer.get_tuple(config)

            if not checkpoint_tuple:
                logger.info(f"获取会话历史: {session_id}, 消息数量: 0")
                return []

            # checkpoint_tuple 可能是命名元组或普通元组，安全地提取 checkpoint
            # 通常第一个元素是 checkpoint 数据
            if hasattr(checkpoint_tuple, "checkpoint"):
                checkpoint_data = checkpoint_tuple.checkpoint  # type: ignore
            else:
                # 如果是普通元组，第一个元素是 checkpoint
                checkpoint_data = checkpoint_tuple[0] if checkpoint_tuple else {}

            # 从检查点中提取消息
            messages = checkpoint_data.get("channel_values", {}).get("messages", [])

            # 转换为前端需要的格式
            history = []
            for msg in messages:
                # 跳过系统消息
                if isinstance(msg, SystemMessage):
                    continue

                role = "user" if isinstance(msg, HumanMessage) else "assistant"
                content = msg.content if hasattr(msg, "content") else str(msg)

                # 提取时间戳（如果有的话）
                timestamp = getattr(msg, "timestamp", None)
                if timestamp:
                    history.append({"role": role, "content": content, "timestamp": timestamp})
                else:
                    from datetime import datetime

                    history.append(
                        {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
                    )

            logger.info(f"获取会话历史: {session_id}, 消息数量: {len(history)}")
            return history

        except Exception as e:
            logger.error(f"获取会话历史失败: {session_id}, 错误: {e}")
            return []

    async def clear_session(self, session_id: str) -> bool:
        """
        清空会话历史（从 MemorySaver checkpointer 中删除）

        Args:
            session_id: 会话ID（即 thread_id）

        Returns:
            bool: 是否成功
        """
        try:
            # 使用 checkpointer 的 delete_thread 方法删除该 thread 的所有检查点
            if hasattr(self.checkpointer, "adelete_thread"):
                await self.checkpointer.adelete_thread(session_id)
            else:
                self.checkpointer.delete_thread(session_id)

            logger.info(f"已清除会话历史: {session_id}")
            return True

        except Exception as e:
            logger.error(f"清空会话历史失败: {session_id}, 错误: {e}")
            return False

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("清理 RAG Agent 服务资源...")
            # MCP 客户端由全局管理器统一管理，无需手动清理
            logger.info("RAG Agent 服务资源已清理")
        except Exception as e:
            logger.error(f"清理资源失败: {e}")


# 全局单例 - 启用流式输出
rag_agent_service = RagAgentService(streaming=True)
