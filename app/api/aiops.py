"""
AIOps 智能运维接口
"""

import json

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.capacity import AgentCapacityError, agent_capacity  # noqa: F401
from app.coordination import coordination_runtime
from app.models.aiops import AIOpsRequest
from app.security import scoped_session_id
from app.services.aiops_service import aiops_service

router = APIRouter()


@router.post("/aiops")
async def diagnose_stream(payload: AIOpsRequest, request: Request):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 自动获取当前系统的活动告警
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const eventSource = new EventSource('/api/aiops');

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'plan') {
        console.log('诊断计划:', data.plan);
      } else if (data.type === 'step_complete') {
        console.log('步骤完成:', data.current_step);
      } else if (data.type === 'report') {
        console.log('最终报告:', data.report);
      } else if (data.type === 'complete') {
        console.log('诊断完成');
        eventSource.close();
      }
    };
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    # session_id 用于日志关联与 LangGraph thread_id 复用：提供时同一 session 的
    # 多次诊断共享同一 checkpoint 槽位（每轮执行前会清空旧 checkpoint，避免状态污染）；
    # 未提供时由 aiops_service 生成 uuid4 thread_id
    public_session_id = payload.session_id or "anonymous"
    session_id = scoped_session_id(request, public_session_id)
    operation_key = (
        request.headers.get("X-Idempotency-Key")
        or getattr(request.state, "request_id", public_session_id)
    )[:128]
    stream_key = f"aiops:{session_id}:{operation_key}"
    try:
        after_id = max(0, int(request.headers.get("Last-Event-ID", "0")))
    except ValueError:
        after_id = 0
    replay = await coordination_runtime.replay.replay(stream_key, after_id)
    if await coordination_runtime.replay.is_terminal(stream_key):

        async def replay_generator():
            for stored in replay:
                yield stored.as_sse()

        return EventSourceResponse(replay_generator())
    logger.info(f"[会话 {public_session_id}] 收到 AIOps 诊断请求（流式）")
    try:
        capacity_lease = await coordination_runtime.capacity.acquire()
    except AgentCapacityError as exc:
        raise HTTPException(
            status_code=429,
            detail="agent_capacity_exhausted",
            headers={"Retry-After": "2"},
        ) from exc

    # 同一 stream_key 已有活跃 producer 时拒绝并发重连，避免重复执行整个诊断
    producer_lease = await coordination_runtime.replay.try_acquire(stream_key)
    if producer_lease is None:
        await coordination_runtime.capacity.release(capacity_lease)
        raise HTTPException(status_code=409, detail="stream_already_active")

    async def event_generator():
        try:
            for stored in replay:
                yield stored.as_sse()
            async for event in aiops_service.diagnose(session_id=session_id):
                # 发送事件
                terminal = event.get("type") in ["complete", "error"]
                yield (
                    await coordination_runtime.replay.publish(
                        stream_key,
                        json.dumps(event, ensure_ascii=False),
                        terminal=terminal,
                    )
                ).as_sse()

                # 如果是完成或错误事件，结束流
                if event.get("type") in ["complete", "error"]:
                    break

            logger.info(f"[会话 {public_session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(
                f"[会话 {public_session_id}] AIOps 诊断流式响应异常: {type(e).__name__}",
                exc_info=True,
            )
            data = json.dumps(
                {"type": "error", "stage": "exception", "message": f"诊断异常: {str(e)}"},
                ensure_ascii=False,
            )
            yield (
                await coordination_runtime.replay.publish(stream_key, data, terminal=True)
            ).as_sse()
        finally:
            await coordination_runtime.capacity.release(capacity_lease)
            await coordination_runtime.replay.release(stream_key, producer_lease)

    try:
        return EventSourceResponse(event_generator())
    except Exception:
        # EventSourceResponse 创建失败时兜底释放，避免 producer 登记泄漏
        await coordination_runtime.replay.release(stream_key, producer_lease)
        await coordination_runtime.capacity.release(capacity_lease)
        raise
