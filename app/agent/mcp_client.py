"""
MCP 客户端管理
提供全局单例的 MCP 客户端，避免重复初始化
"""

import asyncio
import json
import random
import time
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.interceptors import MCPToolCallRequest
from loguru import logger
from mcp.types import CallToolResult, TextContent

from app.config import config
from app.observability import tool_metrics
from app.observability.tracing import dependency_span
from app.reliability import BulkheadFullError, CircuitOpenError, dependency_guards

# MCP 客户端按服务配置和拦截器组合缓存，避免初始化顺序丢失重试能力。
_mcp_clients: dict[tuple[str, tuple[int, ...]], MultiServerMCPClient] = {}
_mcp_client_lock = asyncio.Lock()


async def retry_interceptor(
    request: MCPToolCallRequest,
    handler,
    max_retries: int = 3,
    delay: float = 1.0,
):
    """MCP 工具调用重试拦截器

    当工具调用失败时，使用指数退避策略自动重试。
    如果所有重试都失败，返回包含错误信息的结果而不是抛出异常。

    MCPToolCallRequest 结构：
    - name: str - 工具名称
    - args: dict[str, Any] - 工具参数
    - server_name: str - 服务器名称

    Args:
        request: MCP 工具调用请求
        handler: 实际的工具调用处理器
        max_retries: 最大重试次数（默认3次）
        delay: 初始延迟时间（秒，默认1秒）

    Returns:
        CallToolResult: 工具调用结果或错误信息
    """
    if max_retries < 1:
        raise ValueError("max_retries 必须大于等于 1")
    if delay < 0:
        raise ValueError("delay 不能小于 0")

    last_error: Exception | None = None
    last_error_result: CallToolResult | None = None
    started = time.perf_counter()
    guard = dependency_guards.get(f"mcp:{request.server_name}")

    for attempt in range(max_retries):
        try:
            logger.info(
                f"调用 MCP 工具: {request.name} "
                f"(服务器: {request.server_name}, 第 {attempt + 1}/{max_retries} 次尝试)"
            )
            with dependency_span(
                f"mcp.{request.name}",
                {
                    "rpc.system": "mcp",
                    "rpc.service": request.server_name,
                    "rpc.method": request.name,
                    "retry.attempt": attempt + 1,
                },
            ):
                # 为单次工具执行设置总超时，超时按失败处理并进入退避重试
                result = await asyncio.wait_for(
                    guard.call(
                        lambda: handler(request),
                        failure_predicate=lambda item: bool(item.isError),
                        exception_failure_predicate=lambda exc: not isinstance(
                            exc, (PermissionError, ValueError, TypeError)
                        ),
                    ),
                    timeout=config.mcp_tool_timeout_seconds,
                )
            if not bool(result.isError):
                latency_ms = (time.perf_counter() - started) * 1000
                tool_metrics.record(request.name, success=True, latency_ms=latency_ms)
                logger.info(f"MCP 工具 {request.name} 调用成功, latency_ms={latency_ms:.2f}")
                return result

            last_error_result = result
            logger.warning(
                f"MCP 工具 {request.name} 返回错误结果 "
                f"(第 {attempt + 1}/{max_retries} 次)"
            )

        except Exception as e:
            last_error = e
            logger.warning(
                f"MCP 工具 {request.name} 调用失败 (第 {attempt + 1}/{max_retries} 次): {str(e)}"
            )
            # 参数类错误与熔断/舱壁拒绝没有重试意义，直接结束
            if isinstance(
                e,
                (PermissionError, ValueError, TypeError, CircuitOpenError, BulkheadFullError),
            ):
                break

        # 异常和 isError=True 都属于失败尝试，统一执行指数退避。
        if attempt < max_retries - 1:
            wait_time = delay * (2**attempt) + random.uniform(0, delay * 0.25)
            logger.info(f"等待 {wait_time:.1f} 秒后重试...")
            await asyncio.sleep(wait_time)

    # 所有重试都失败，返回错误结果而不是抛出异常
    error_detail = str(last_error) if last_error is not None else "工具返回 isError=true"
    error_msg = f"工具 {request.name} 在 {max_retries} 次尝试后仍然失败: {error_detail}"
    tool_metrics.record(
        request.name,
        success=False,
        latency_ms=(time.perf_counter() - started) * 1000,
        error_class=type(last_error).__name__
        if last_error is not None
        else "remote_error",
    )
    logger.error(error_msg)
    if last_error_result is not None:
        return last_error_result
    return CallToolResult(content=[TextContent(type="text", text=error_msg)], isError=True)


# 使用配置文件中定义的完整 MCP 服务器配置
DEFAULT_MCP_SERVERS = config.mcp_servers


async def get_mcp_client(
    servers: dict[str, dict[str, str]] | None = None,
    tool_interceptors: list | None = None,
    force_new: bool = False,
) -> MultiServerMCPClient:
    """
    获取或初始化 MCP 客户端（不带重试拦截器）

    这是一个单例模式，确保整个应用只有一个 MCP 客户端实例（除非 force_new=True）

    从 langchain-mcp-adapters 0.1.0 开始，MultiServerMCPClient 不再支持作为上下文管理器使用。
    直接创建实例即可使用。

    Args:
        servers: MCP 服务器配置，默认使用 DEFAULT_MCP_SERVERS
        tool_interceptors: 自定义工具拦截器列表
        force_new: 是否强制创建新实例（用于特殊场景，如需要不同配置）

    Returns:
        MultiServerMCPClient: MCP 客户端实例
    """
    # 如果请求新实例，直接创建并返回（不缓存）
    if force_new:
        logger.info("创建新的 MCP 客户端实例（非单例）")
        client = _create_mcp_client(servers or DEFAULT_MCP_SERVERS, tool_interceptors)
        # 不再需要 __aenter__()，直接返回即可
        return client

    selected_servers = servers or DEFAULT_MCP_SERVERS
    cache_key = (
        json.dumps(selected_servers, ensure_ascii=True, sort_keys=True),
        tuple(id(interceptor) for interceptor in (tool_interceptors or [])),
    )
    cached = _mcp_clients.get(cache_key)
    if cached is not None:
        return cached

    async with _mcp_client_lock:
        cached = _mcp_clients.get(cache_key)
        if cached is None:
            logger.info("初始化全局 MCP 客户端...")
            cached = _create_mcp_client(selected_servers, tool_interceptors)
            _mcp_clients[cache_key] = cached
            logger.info("全局 MCP 客户端初始化完成")
        return cached


async def get_mcp_client_with_retry(
    servers: dict[str, dict[str, str]] | None = None,
    tool_interceptors: list | None = None,
    force_new: bool = False,
) -> MultiServerMCPClient:
    """
    获取或初始化带重试功能的 MCP 客户端

    这是一个单例模式，确保整个应用只有一个 MCP 客户端实例（除非 force_new=True）
    重试拦截器会自动添加到拦截器列表的开头

    Args:
        servers: MCP 服务器配置，默认使用 DEFAULT_MCP_SERVERS
        tool_interceptors: 自定义工具拦截器列表（会在重试拦截器之后添加）
        force_new: 是否强制创建新实例（用于特殊场景，如需要不同配置）

    Returns:
        MultiServerMCPClient: 带重试功能的 MCP 客户端实例
    """
    # 构建拦截器列表：重试拦截器在最前面
    interceptors = [retry_interceptor]
    if tool_interceptors:
        interceptors.extend(tool_interceptors)

    return await get_mcp_client(
        servers=servers, tool_interceptors=interceptors, force_new=force_new
    )


def _create_mcp_client(
    servers: dict[str, dict[str, str]], tool_interceptors: list | None = None
) -> MultiServerMCPClient:
    """
    创建 MCP 客户端实例

    Args:
        servers: MCP 服务器配置
        tool_interceptors: 工具拦截器列表

    Returns:
        MultiServerMCPClient: 未初始化的客户端实例
    """
    # MultiServerMCPClient 的第一个参数直接接收 servers 配置字典
    # 格式: {server_name: {"transport": "...", "url": "..."}}
    kwargs: dict[str, Any] = {}

    if tool_interceptors:
        kwargs["tool_interceptors"] = tool_interceptors

    # 第一个参数是 servers 配置，直接传递
    return MultiServerMCPClient(servers, **kwargs)  # type: ignore[arg-type]
