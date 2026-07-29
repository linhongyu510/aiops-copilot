"""对话接口

提供基于 RAG Agent 的普通对话和流式对话接口
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.capacity import AgentCapacityError, agent_capacity
from app.config import config
from app.models.request import ChatRequest, ClearRequest
from app.models.response import ApiResponse, SessionInfoResponse
from app.security import scoped_session_id
from app.services.rag_agent_service import rag_agent_service
from app.sse import sse_replay_store

router = APIRouter()


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request):
    """快速对话接口
    {
        "code": 200,
        "message": "success",
        "data": {
            "success": true,
            "answer": "回答内容",
            "errorMessage": null
        }
    }

    Args:
        request: 对话请求

    Returns:
        统一格式的对话响应
    """
    acquired = False
    try:
        await agent_capacity.acquire()
        acquired = True
        session_id = scoped_session_id(request, payload.id)
        logger.info(f"[会话 {payload.id}] 收到快速对话请求, chars={len(payload.question)}")
        # 为整个对话处理设置总时间预算，超时返回 504 而不是无限等待
        answer = await asyncio.wait_for(
            rag_agent_service.query(payload.question, session_id=session_id),
            timeout=config.chat_total_timeout_seconds,
        )

        logger.info(f"[会话 {payload.id}] 快速对话完成")

        return {
            "code": 200,
            "message": "success",
            "data": {"success": True, "answer": answer, "errorMessage": None},
        }

    except AgentCapacityError:
        return JSONResponse(
            status_code=429,
            content={"code": 429, "message": "agent_capacity_exhausted", "data": None},
            headers={"Retry-After": "2"},
        )
    except TimeoutError:
        logger.warning(f"[会话 {payload.id}] 快速对话超出总时间预算")
        return JSONResponse(
            status_code=504,
            content={
                "code": 504,
                "message": "chat_timeout",
                "data": {
                    "success": False,
                    "answer": None,
                    "errorMessage": "对话处理超时，请缩小问题范围后重试",
                },
            },
        )
    except Exception as e:
        logger.error(f"对话接口错误: {e}")
        return JSONResponse(
            status_code=502,
            content={
                "code": 502,
                "message": "model_service_error",
                "data": {
                    "success": False,
                    "answer": None,
                    "errorMessage": "模型服务调用失败，请检查服务配置和日志",
                },
            },
        )
    finally:
        if acquired:
            agent_capacity.release()


@router.post("/chat_stream")
async def chat_stream(payload: ChatRequest, request: Request):
    """流式对话接口（基于 RAG Agent，SSE）

    返回 SSE 格式，data 字段为 JSON：

    工具调用事件:
    event: message
    data: {"type":"tool_call","data":{"tool":"工具名","status":"start|end","input":{...}}}

    内容流式事件:
    event: message
    data: {"type":"content","data":"内容块"}

    完成事件:
    event: message
    data: {"type":"done","data":{"answer":"完整答案","tool_calls":[...]}}

    Args:
        request: 对话请求

    Returns:
        SSE 事件流
    """
    session_id = scoped_session_id(request, payload.id)
    operation_key = (
        request.headers.get("X-Idempotency-Key")
        or getattr(request.state, "request_id", payload.id)
    )[:128]
    stream_key = f"chat:{session_id}:{operation_key}"
    try:
        after_id = max(0, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError:
        after_id = 0
    replay = sse_replay_store.replay(stream_key, after_id)
    terminal_replay = sse_replay_store.is_terminal(stream_key)
    logger.info(f"[会话 {payload.id}] 收到流式对话请求, chars={len(payload.question)}")
    if terminal_replay:
        async def replay_generator():
            for stored in replay:
                yield stored.as_sse()

        return EventSourceResponse(replay_generator())

    try:
        await agent_capacity.acquire()
    except AgentCapacityError as exc:
        raise HTTPException(
            status_code=429,
            detail="agent_capacity_exhausted",
            headers={"Retry-After": "2"},
        ) from exc

    # 同一 stream_key 已有活跃 producer 时拒绝并发重连，避免重复执行整个 Agent
    if not sse_replay_store.try_acquire(stream_key):
        agent_capacity.release()
        raise HTTPException(status_code=409, detail="stream_already_active")
    async def event_generator():
        try:
            for stored in replay:
                yield stored.as_sse()
            try:
                # 为整个流式执行设置总时间预算，超时发布 terminal error 事件后停止
                async with asyncio.timeout(config.chat_total_timeout_seconds):
                    async for chunk in rag_agent_service.query_stream(
                        payload.question, session_id=session_id
                    ):
                        chunk_type = chunk.get("type", "unknown")
                        chunk_data = chunk.get("data", None)

                        # 处理调试类型消息（新增）
                        if chunk_type == "debug":
                            # 调试信息，可以选择发送或忽略
                            data = json.dumps(
                                {
                                    "type": "debug",
                                    "node": chunk.get("node", "unknown"),
                                    "message_type": chunk.get("message_type", "unknown"),
                                },
                                ensure_ascii=False,
                            )
                            yield sse_replay_store.publish(stream_key, data).as_sse()
                        elif chunk_type == "tool_call":
                            # 发送工具调用事件（可选，前端可以显示工具调用状态）
                            data = json.dumps(
                                {"type": "tool_call", "data": chunk_data}, ensure_ascii=False
                            )
                            yield sse_replay_store.publish(stream_key, data).as_sse()
                        elif chunk_type == "search_results":
                            # 发送检索结果（可选，前端可以忽略）
                            data = json.dumps(
                                {"type": "search_results", "data": chunk_data}, ensure_ascii=False
                            )
                            yield sse_replay_store.publish(stream_key, data).as_sse()
                        elif chunk_type == "content":
                            # 发送内容块 - 关键：data 必须是 JSON 字符串
                            data = json.dumps(
                                {"type": "content", "data": chunk_data}, ensure_ascii=False
                            )
                            yield sse_replay_store.publish(stream_key, data).as_sse()
                        elif chunk_type == "complete":
                            # 发送完成信号
                            data = json.dumps(
                                {"type": "done", "data": chunk_data}, ensure_ascii=False
                            )
                            yield sse_replay_store.publish(
                                stream_key, data, terminal=True
                            ).as_sse()
                        elif chunk_type == "error":
                            # 发送错误信息
                            data = json.dumps(
                                {"type": "error", "data": str(chunk_data)}, ensure_ascii=False
                            )
                            yield sse_replay_store.publish(
                                stream_key, data, terminal=True
                            ).as_sse()

                logger.info(f"[会话 {payload.id}] 流式对话完成")
            except TimeoutError:
                logger.warning(f"[会话 {payload.id}] 流式对话超出总时间预算")
                data = json.dumps(
                    {"type": "error", "data": "对话处理超时，请缩小问题范围后重试"},
                    ensure_ascii=False,
                )
                yield sse_replay_store.publish(stream_key, data, terminal=True).as_sse()

        except Exception as e:
            logger.error(f"流式对话接口错误: {e}")
            data = json.dumps({"type": "error", "data": str(e)}, ensure_ascii=False)
            yield sse_replay_store.publish(stream_key, data, terminal=True).as_sse()
        finally:
            agent_capacity.release()
            sse_replay_store.release(stream_key)

    try:
        return EventSourceResponse(event_generator())
    except Exception:
        # EventSourceResponse 创建失败时兜底释放，避免 producer 登记泄漏
        sse_replay_store.release(stream_key)
        agent_capacity.release()
        raise


@router.post("/chat/clear", response_model=ApiResponse)
async def clear_session(payload: ClearRequest, request: Request):
    """清空会话历史

    Args:
        request: 清空请求

    Returns:
        操作结果
    """
    try:
        session_id = scoped_session_id(request, payload.session_id)
        success = await rag_agent_service.clear_session(session_id)
        logger.info(f"清空会话: {payload.session_id}, 结果: {success}")

        return ApiResponse(
            status="success" if success else "error",
            message="会话已清空" if success else "清空会话失败",
            data=None,
        )

    except Exception as e:
        logger.error(f"清空会话错误: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/chat/session/{session_id}", response_model=SessionInfoResponse)
async def get_session_info(session_id: str, request: Request) -> SessionInfoResponse:
    """查询会话历史

    Args:
        session_id: 会话 ID

    Returns:
        会话信息
    """
    try:
        scoped_id = scoped_session_id(request, session_id)
        history = await rag_agent_service.get_session_history(scoped_id)

        return SessionInfoResponse(
            session_id=session_id, message_count=len(history), history=history
        )

    except Exception as e:
        logger.error(f"获取会话信息错误: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
