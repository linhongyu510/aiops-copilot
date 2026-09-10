"""健康检查接口"""

from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from loguru import logger

from app.checkpointing import checkpoint_runtime
from app.config import config
from app.coordination import coordination_runtime
from app.observability.tracing import telemetry_status
from app.reliability import dependency_guards
from app.services.retrieval_backend import get_retrieval_backend

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查接口
    检查服务状态和检索后端连接状态

    Returns:
        JSONResponse: 健康检查结果
    """
    # 检查服务基本状态
    health_data: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "service": config.app_name,
        "version": config.app_version,
        "status": "healthy",
        "llm": {
            "provider": config.llm_provider,
            "model": config.rag_model,
            "reasoning_model": config.llm_reasoning_model,
            "configured": bool(config.llm_api_key),
        },
    }

    backend = get_retrieval_backend()
    health_data["retrieval_backend"] = backend.backend_type

    retrieval_healthy = False
    if backend.backend_type == "local_wiki":
        wiki_health = await run_in_threadpool(backend.health)
        health_data["local_wiki"] = wiki_health
        health_data["milvus"] = {
            "status": "disabled",
            "message": "未启用 Milvus 后端",
        }
        retrieval_healthy = wiki_health.get("status") == "ready"
    else:
        # Milvus 后端：保留原有的连接检查逻辑
        try:
            milvus_healthy = await run_in_threadpool(backend.health)
            milvus_status: str = milvus_healthy.get("status", "disconnected")
            milvus_message: str = milvus_healthy.get("message", "Milvus 检查失败")
            health_data["milvus"] = {"status": milvus_status, "message": milvus_message}
        except Exception as e:
            logger.warning(f"Milvus 健康检查失败: {e}")
            milvus_status = "error"
            health_data["milvus"] = {"status": "error", "message": f"Milvus 检查失败: {str(e)}"}
        health_data["local_wiki"] = {
            "status": "disabled",
            "message": "未启用本地 Wiki",
        }
        retrieval_healthy = milvus_status == "connected"

    # 判断整体健康状态
    overall_status = "healthy" if retrieval_healthy else "unhealthy"
    status_code = 200 if retrieval_healthy else 503

    if not retrieval_healthy:
        if backend.backend_type == "local_wiki":
            health_data["error"] = "本地 Wiki 未就绪"
        else:
            health_data["error"] = "数据库不可用"

    health_data["status"] = overall_status

    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": "服务运行正常" if overall_status == "healthy" else "服务不可用",
            "data": health_data,
        },
    )


@router.get("/live")
async def liveness_check():
    """Liveness only proves that the API process and event loop are responsive."""
    return {"status": "alive", "service": config.app_name, "version": config.app_version}


@router.get("/ready")
async def readiness_check():
    """Readiness includes the vector store required by knowledge-backed requests."""
    return await health_check()


@router.get("/production/readiness")
async def production_readiness():
    """Expose deployment blockers without claiming a local demo is production-ready."""
    origins = config.cors_origin_list
    checks = {
        "authentication": {
            "ok": config.auth_enabled and bool(config.api_key_roles),
            "detail": "enabled" if config.auth_enabled else "disabled",
        },
        "cors_restricted": {
            "ok": bool(origins) and "*" not in origins,
            "detail": origins,
        },
        "debug_disabled": {"ok": not config.debug, "detail": f"debug={config.debug}"},
        "persistent_sessions": {
            "ok": checkpoint_runtime.backend == "postgres",
            "detail": checkpoint_runtime.backend,
        },
        "distributed_metrics": {
            "ok": (
                config.metrics_backend.strip().lower() == "prometheus"
                and bool(config.prometheus_url)
            ),
            "detail": (
                config.prometheus_url
                if config.metrics_backend.strip().lower() == "prometheus"
                else "process_local_registry"
            ),
        },
        "distributed_coordination": {
            "ok": coordination_runtime.backend == "redis",
            "detail": coordination_runtime.backend,
        },
        "distributed_tracing": {
            "ok": telemetry_status()["exporter_configured"],
            "detail": telemetry_status()["endpoint"] or "disabled",
        },
    }
    blockers = [name for name, item in checks.items() if not item["ok"]]
    return {
        "ready_for_production": not blockers,
        "checks": checks,
        "blockers": blockers,
        "dependency_guards": dependency_guards.snapshots(),
    }
