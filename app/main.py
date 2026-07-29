"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.api import aiops, chat, file, health, metrics
from app.checkpointing import checkpoint_runtime
from app.config import config
from app.coordination import coordination_runtime
from app.core.milvus_client import milvus_manager
from app.observability import request_metrics
from app.observability.tracing import configure_telemetry, shutdown_telemetry
from app.security import required_role, resolve_identity, role_allows

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")

    checkpointer = await checkpoint_runtime.start()
    chat.rag_agent_service.configure_checkpointer(checkpointer)
    aiops.aiops_service.configure_checkpointer(checkpointer)
    await coordination_runtime.start()

    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    try:
        await run_in_threadpool(milvus_manager.connect)
        logger.info("✅ Milvus 连接成功")
    except Exception as exc:
        logger.warning(f"Milvus 启动连接失败，非 RAG 诊断仍可使用: {exc}")
        if config.milvus_required_on_startup:
            raise

    logger.info("=" * 60)

    try:
        yield
    finally:
        # 关闭时执行
        logger.info("🔌 正在关闭持久化与 Milvus 连接...")
        await coordination_runtime.close()
        await checkpoint_runtime.close()
        milvus_manager.close()
        shutdown_telemetry()
        logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan,
)
configure_telemetry(app)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def operational_middleware(request: Request, call_next):
    """Attach request identity/id, enforce roles and collect route metrics."""
    started = time.perf_counter()
    request_metrics.begin()
    request_id = (request.headers.get("X-Request-ID") or uuid.uuid4().hex)[:128]
    request.state.request_id = request_id
    status_code = 500
    try:
        identity = resolve_identity(request)
        required = required_role(request.url.path, request.method)
        if config.auth_enabled and not config.api_key_roles and required:
            response = JSONResponse(status_code=503, content={"detail": "auth_not_configured"})
        elif required and identity is None:
            response = JSONResponse(status_code=401, content={"detail": "authentication_required"})
        elif required and identity is not None and not role_allows(identity.role, required):
            response = JSONResponse(status_code=403, content={"detail": "insufficient_role"})
        else:
            request.state.identity = identity
            response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_metrics.end(
            request.method,
            request.url.path,
            status_code,
            (time.perf_counter() - started) * 1000,
        )


# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(metrics.router, prefix="/api", tags=["可观测性"])

# 挂载静态文件
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        "docs": "/docs",
    }


if __name__ == "__main__":
    from app.run import main

    main()
