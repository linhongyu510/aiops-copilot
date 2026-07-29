"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    # 使用 AIOPS_ 前缀，避免被宿主机常见的 DEBUG/HOST/PORT 环境变量污染。
    app_name: str = Field("AIOps Copilot", validation_alias="AIOPS_APP_NAME")
    app_version: str = "1.4.0"
    debug: bool = Field(False, validation_alias="AIOPS_DEBUG")
    host: str = Field("0.0.0.0", validation_alias="AIOPS_HOST")
    port: int = Field(9900, validation_alias="AIOPS_PORT")

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # 通用 OpenAI 兼容 LLM 配置；默认使用本机 Ollama，避免在线额度影响演示与评测。
    llm_provider: str = "ollama"
    llm_model: str = "qwen3:8b"
    llm_api_base: str = "http://127.0.0.1:11434/v1"
    llm_api_key: str = "ollama"
    llm_timeout_seconds: int = 180
    llm_max_tokens: int = 320
    llm_max_retries: int = 2
    # 独立保留复杂报告模型；Agent 工具循环仍使用 rag_model 的非思考模式。
    llm_reasoning_model: str = "deepseek-v4-pro"

    # Agent 执行配置
    agent_step_max_iterations: int = 5  # Executor 单步骤内 LLM-工具 循环的最大轮数
    aiops_recursion_limit: int = 50  # AIOps 诊断图的 LangGraph recursion_limit
    aiops_total_timeout_seconds: int = 1800  # 单次 AIOps 诊断的总时间预算（秒）
    max_concurrent_agent_requests: int = Field(
        8, validation_alias="AIOPS_MAX_CONCURRENT_AGENT_REQUESTS"
    )
    request_queue_timeout_seconds: float = Field(
        1.0, validation_alias="AIOPS_REQUEST_QUEUE_TIMEOUT_SECONDS"
    )
    # 对话路径预算：/api/chat 总超时与 chat ReAct Agent 的 LangGraph recursion_limit
    chat_total_timeout_seconds: float = Field(
        300.0, validation_alias="AIOPS_CHAT_TOTAL_TIMEOUT_SECONDS"
    )
    chat_recursion_limit: int = Field(12, validation_alias="AIOPS_CHAT_RECURSION_LIMIT")

    # HTTP 安全与部署配置
    cors_origins: str = Field(
        "http://127.0.0.1:9900,http://localhost:9900",
        validation_alias="AIOPS_CORS_ORIGINS",
    )
    auth_enabled: bool = Field(False, validation_alias="AIOPS_AUTH_ENABLED")
    # 逗号分隔的 secret:role；role 支持 viewer/operator/admin。
    api_keys: str = Field("", validation_alias="AIOPS_API_KEYS")
    milvus_required_on_startup: bool = Field(
        False, validation_alias="AIOPS_MILVUS_REQUIRED_ON_STARTUP"
    )
    checkpoint_backend: str = Field("memory", validation_alias="AIOPS_CHECKPOINT_BACKEND")
    checkpoint_postgres_dsn: str = Field("", validation_alias="AIOPS_CHECKPOINT_POSTGRES_DSN")
    checkpoint_required: bool = Field(False, validation_alias="AIOPS_CHECKPOINT_REQUIRED")
    coordination_backend: str = Field("memory", validation_alias="AIOPS_COORDINATION_BACKEND")
    redis_url: str = Field("redis://127.0.0.1:6389/0", validation_alias="AIOPS_REDIS_URL")
    coordination_required: bool = Field(False, validation_alias="AIOPS_COORDINATION_REQUIRED")
    coordination_key_prefix: str = Field("aiops", validation_alias="AIOPS_COORDINATION_KEY_PREFIX")

    # OpenTelemetry / SLO
    otel_enabled: bool = Field(False, validation_alias="AIOPS_OTEL_ENABLED")
    otel_service_name: str = Field("aiops-copilot", validation_alias="OTEL_SERVICE_NAME")
    otel_exporter_otlp_endpoint: str = Field("", validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT")
    metrics_backend: str = Field("process", validation_alias="AIOPS_METRICS_BACKEND")
    prometheus_url: str = Field("", validation_alias="AIOPS_PROMETHEUS_URL")
    tool_slo_success_rate: float = Field(0.99, validation_alias="AIOPS_TOOL_SLO_SUCCESS_RATE")
    tool_slo_p95_ms: float = Field(5000.0, validation_alias="AIOPS_TOOL_SLO_P95_MS")

    # Dependency isolation / circuit breaking
    dependency_max_concurrency: int = Field(8, validation_alias="AIOPS_DEPENDENCY_MAX_CONCURRENCY")
    dependency_queue_timeout_seconds: float = Field(
        0.5, validation_alias="AIOPS_DEPENDENCY_QUEUE_TIMEOUT_SECONDS"
    )
    circuit_failure_threshold: int = Field(3, validation_alias="AIOPS_CIRCUIT_FAILURE_THRESHOLD")
    circuit_recovery_timeout_seconds: float = Field(
        10.0, validation_alias="AIOPS_CIRCUIT_RECOVERY_TIMEOUT_SECONDS"
    )
    sse_replay_events: int = Field(256, validation_alias="AIOPS_SSE_REPLAY_EVENTS")
    # SSE 重放缓存治理：terminal key 超过 TTL 惰性删除，key 总数超限按最久未使用驱逐
    sse_replay_ttl_seconds: float = Field(600.0, validation_alias="AIOPS_SSE_REPLAY_TTL_SECONDS")
    sse_replay_max_keys: int = Field(1000, validation_alias="AIOPS_SSE_REPLAY_MAX_KEYS")

    # 默认使用适合 CPU 演示的开源中文 BGE Small，DashScope 保留为可选后端。
    embedding_provider: str = "local"
    embedding_dimensions: int = 512
    local_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    local_embedding_device: str = "auto"
    local_embedding_batch_size: int = 8
    local_embedding_cache_dir: str = ""
    local_embedding_source: str = "modelscope"
    modelscope_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    huggingface_endpoint: str = ""

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒
    milvus_collection_name: str = "biz_bge_small_zh"

    # RAG 配置
    rag_top_k: int = 5
    rag_model: str = "qwen3:8b"

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100
    rag_splitter_strategy: str = "plain"

    # MCP 服务配置
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"
    mcp_ops_transport: str = "streamable-http"
    mcp_ops_url: str = "http://localhost:8005/mcp"
    # MCP 工具单次执行超时（秒），超时按失败处理并进入退避重试
    mcp_tool_timeout_seconds: float = Field(30.0, validation_alias="AIOPS_MCP_TOOL_TIMEOUT_SECONDS")

    # Ops MCP 工具配置。数据库账号应使用只读账号。
    mysql_dsn: str = ""
    mysql_allowed_schemas: str = ""
    mysql_query_timeout_seconds: int = 10
    mysql_max_rows: int = 200
    tavily_api_key: str = ""
    web_search_max_results: int = 5

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug_mode(cls, value: Any) -> Any:
        """兼容部署平台常见的 dev/prod/release 环境命名。"""
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"dev", "debug", "development"}:
                return True
            if normalized in {"prod", "production", "release"}:
                return False
        return value

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
            "ops": {
                "transport": self.mcp_ops_transport,
                "url": self.mcp_ops_url,
            },
        }

    @property
    def cors_origin_list(self) -> list[str]:
        """返回去重后的显式 CORS 来源。"""
        origins = [item.strip() for item in self.cors_origins.split(",") if item.strip()]
        return list(dict.fromkeys(origins))

    @property
    def api_key_roles(self) -> dict[str, str]:
        """解析 secret:role 列表，忽略格式错误或不支持的角色。"""
        roles: dict[str, str] = {}
        for item in self.api_keys.split(","):
            secret, separator, role = item.strip().rpartition(":")
            normalized_role = role.strip().lower()
            if separator and secret.strip() and normalized_role in {"viewer", "operator", "admin"}:
                roles[secret.strip()] = normalized_role
        return roles


# 全局配置实例
config = Settings()
