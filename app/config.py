"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理。

需要 ``[server]`` extra（``pydantic-settings``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from typing import Any

from pydantic import Field, field_validator

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.config 需要 `[server]` extra（pydantic-settings）。"
        "\n    pip install 'aiops-copilot[server]'"
    ) from _exc


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
    app_version: str = "2.0.0"
    debug: bool = Field(False, validation_alias="AIOPS_DEBUG")
    host: str = Field("0.0.0.0", validation_alias="AIOPS_HOST")
    port: int = Field(9900, validation_alias="AIOPS_PORT")

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # 默认使用远程快速 LLM，避免 8GB 显卡同时驻留本地 8B 与 BGE 双模型。
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-flash"
    llm_api_base: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_timeout_seconds: int = 180
    llm_max_tokens: int = 320
    llm_max_retries: int = 2
    # LLM 计量单价（美元/百万 token）；默认 0 表示未配置，不累计成本。
    llm_price_input_per_million: float = 0.0
    llm_price_output_per_million: float = 0.0
    # 独立保留复杂报告模型；Agent 工具循环仍使用 rag_model 的非思考模式。
    llm_reasoning_model: str = "deepseek-v4-pro"

    # Agent 执行配置
    agent_step_max_iterations: int = 5  # Executor 单步骤内 LLM-工具 循环的最大轮数
    # Executor 单批次并行执行的就绪步骤上限（DAG 调度，P0.2）
    aiops_max_parallel_steps: int = 3
    # 观测压缩（P0.4）：past_steps 决策视图与 artifacts 报告存档的字符上限
    aiops_observation_max_chars: int = 1200
    aiops_artifact_max_chars: int = 4000
    # 工具输出注入防御（P1.2）：MCP 拦截器统一围栏 + 注入行清除的最大长度
    tool_output_max_chars: int = Field(8000, validation_alias="AIOPS_TOOL_OUTPUT_MAX_CHARS")
    # 变更提案（P1.3）：待审批提案的存活时间（秒），过期作废
    action_proposal_ttl_seconds: float = Field(
        3600.0, validation_alias="AIOPS_ACTION_PROPOSAL_TTL_SECONDS"
    )
    # 诊断链路（Plan-Execute-Replan）每个节点暴露给 LLM 的工具上限；
    # 比 chat 链路（tool_router.MAX_EXPOSED_TOOLS=8）略宽，因为诊断步骤异构
    aiops_max_exposed_tools: int = 12
    aiops_recursion_limit: int = 50  # AIOps 诊断图的 LangGraph recursion_limit
    aiops_total_timeout_seconds: int = 1800  # 单次 AIOps 诊断的总时间预算（秒）
    # Domain Profile（P0-3）：切换 profile 会改变工具关键词分组与 prompt 风格；
    # 内置 aiops profile 与原硬编码常量等价，改名请同步 profiles/<name>.yaml。
    aiops_domain_profile: str = Field(
        "aiops", validation_alias="AIOPS_DOMAIN_PROFILE"
    )
    # 事件记忆（P0.3）：诊断结束自动沉淀 episode，规划前检索相似历史事件
    aiops_incident_memory_enabled: bool = Field(
        True, validation_alias="AIOPS_INCIDENT_MEMORY_ENABLED"
    )
    aiops_incident_memory_path: str = Field(
        ".runtime/incident_memory.jsonl", validation_alias="AIOPS_INCIDENT_MEMORY_PATH"
    )
    aiops_incident_memory_top_k: int = Field(
        2, validation_alias="AIOPS_INCIDENT_MEMORY_TOP_K"
    )
    aiops_incident_memory_max_episodes: int = Field(
        500, validation_alias="AIOPS_INCIDENT_MEMORY_MAX_EPISODES"
    )
    aiops_incident_memory_min_score: float = Field(
        0.15, validation_alias="AIOPS_INCIDENT_MEMORY_MIN_SCORE"
    )
    # 事件接入与自治诊断（P1.1）
    incident_dedup_window_seconds: float = Field(
        300.0, validation_alias="AIOPS_INCIDENT_DEDUP_WINDOW_SECONDS"
    )
    # 服务拓扑（P2.2）：依赖图 JSON 文件路径；空 = 内置演示拓扑（显式标注来源）
    aiops_topology_path: str = Field("", validation_alias="AIOPS_TOPOLOGY_PATH")
    # 预案库（P2.4）：预案 JSONL 路径；空 = 内置演示预案（显式标注来源）
    aiops_playbooks_path: str = Field("", validation_alias="AIOPS_PLAYBOOKS_PATH")
    aiops_playbook_top_k: int = Field(2, validation_alias="AIOPS_PLAYBOOK_TOP_K")
    aiops_playbook_min_score: float = Field(
        0.15, validation_alias="AIOPS_PLAYBOOK_MIN_SCORE"
    )
    incident_autonomous_diagnosis_enabled: bool = Field(
        True, validation_alias="AIOPS_INCIDENT_AUTONOMOUS_DIAGNOSIS_ENABLED"
    )
    incident_max_concurrent_diagnoses: int = Field(
        2, validation_alias="AIOPS_INCIDENT_MAX_CONCURRENT_DIAGNOSES"
    )
    incident_notify_webhook_url: str = Field(
        "", validation_alias="AIOPS_INCIDENT_NOTIFY_WEBHOOK_URL"
    )
    #: 启用的通知渠道，逗号分隔（webhook,slack,feishu）；空串时若配置了
    #: `incident_notify_webhook_url` 会自动回退到单 webhook（legacy 行为）。
    incident_notifiers: str = Field("", validation_alias="AIOPS_INCIDENT_NOTIFIERS")
    incident_slack_webhook_url: str = Field(
        "", validation_alias="AIOPS_INCIDENT_SLACK_WEBHOOK_URL"
    )
    incident_feishu_webhook_url: str = Field(
        "", validation_alias="AIOPS_INCIDENT_FEISHU_WEBHOOK_URL"
    )
    incident_store_max: int = Field(200, validation_alias="AIOPS_INCIDENT_STORE_MAX")
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
    # Pin the tool used by /api/observability/trace-probe. Empty means "pick the
    # first registered read-only tool", so the probe stays vendor-neutral.
    trace_probe_tool: str = Field("", validation_alias="AIOPS_TRACE_PROBE_TOOL")

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

    # RAG 2.0 默认使用本地 BGE Large；模型与 collection 必须保持维度一致。
    embedding_provider: str = "local"
    embedding_dimensions: int = 1024
    local_embedding_model: str = "BAAI/bge-large-zh-v1.5"
    local_embedding_revision: str = "79e7739b6ab944e86d6171e44d24c997fc1e0116"
    local_embedding_device: str = "auto"
    local_embedding_batch_size: int = 8
    local_embedding_cache_dir: str = ""
    local_embedding_source: str = "huggingface"
    modelscope_embedding_model: str = "BAAI/bge-large-zh-v1.5"
    embedding_query_instruction: str = "为这个句子生成表示以用于检索相关文章："
    huggingface_endpoint: str = ""
    # Query embeddings are deterministic for a fixed model, so identical queries
    # (retries, reruns of the same alert, shared runbook phrasing) reuse a cached
    # vector instead of re-encoding. 0 disables the cache.
    embedding_query_cache_size: int = Field(
        512, validation_alias="AIOPS_EMBEDDING_QUERY_CACHE_SIZE"
    )

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒
    milvus_collection_name: str = "aiops_kb_bge_large_zh_v1_5_v1"
    milvus_collection_alias: str = "aiops_kb_current"
    milvus_metric_type: str = "COSINE"

    # RAG 配置
    rag_top_k: int = 5
    rag_model: str = "deepseek-v4-flash"
    rag_expansion_model: str = "deepseek-v4-flash"
    rag_multi_query_count: int = 3
    rag_branch_top_k: int = 20
    rag_rrf_k: int = 60
    rag_rrf_top_k: int = 30
    rag_max_chunks_per_source: int = 2
    rag_expansion_enabled: bool = True
    rag_bm25_enabled: bool = True
    rag_reranker_enabled: bool = True
    rag_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    rag_reranker_revision: str = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
    rag_reranker_device: str = "auto"
    rag_reranker_batch_size: int = 8
    rag_reranker_use_fp16: bool = True
    rag_reranker_cache_dir: str = ""
    rag_min_reranker_score: float = 0.1
    rag_model_max_concurrency: int = 1
    rag_expansion_cache_size: int = 512

    # 文档分块配置
    chunk_max_size: int = 400
    chunk_overlap: int = 64
    rag_splitter_strategy: str = "markdown-header"
    chunk_length_unit: str = "token"
    corpus_version: str = "aiops-runbook-v3"

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
