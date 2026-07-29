"""健康检查接口"""

from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from loguru import logger

from app.checkpointing import checkpoint_runtime
from app.config import config
from app.coordination import coordination_runtime
from app.core.milvus_client import milvus_manager
from app.observability.tracing import telemetry_status
from app.reliability import dependency_guards

router = APIRouter()


@router.get("/health")
async def health_check():
    """健康检查接口
    检查服务状态和数据库连接状态

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

    # 检查 Milvus 连接状态
    try:
        milvus_healthy = await run_in_threadpool(milvus_manager.health_check)
        milvus_status: str = "connected" if milvus_healthy else "disconnected"
        milvus_message: str = "Milvus 连接正常" if milvus_healthy else "Milvus 连接异常"
        health_data["milvus"] = {"status": milvus_status, "message": milvus_message}
    except Exception as e:
        logger.warning(f"Milvus 健康检查失败: {e}")
        health_data["milvus"] = {"status": "error", "message": f"Milvus 检查失败: {str(e)}"}

    # 判断整体健康状态
    overall_status = "healthy"
    status_code = 200

    # 如果 Milvus 不可用，服务不可用
    if health_data["milvus"]["status"] != "connected":
        overall_status = "unhealthy"
        status_code = 503
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
