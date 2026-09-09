"""工具注册中心：治理元数据 + 语义路由。

设计目标（P0.1）：
1. 单一事实来源：36 个工具（34 MCP + 2 本地）的分类、只读性、风险等级、
   所需角色、关键词与补充描述集中注册，供路由、工具级 RBAC 与后续的
   ActionGovernor 变更审批复用；
2. 语义泛化：问题命中领域关键词时保持确定性路由（行为与旧版一致）；
   未命中任何领域关键词时，退化为字符 n-gram TF-IDF 余弦相似度检索，
   使「服务响应变慢」这类不含领域关键词的问法也能命中 probe_http /
   prom_query_range 等工具；
3. 无模型依赖、结果确定、可离线单测；后续可将相似度层替换为
   embedding 检索而不改动调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.text_similarity import TfidfIndex, tokenize as _tokenize  # noqa: F401 (复用共享实现)

# 语义泛化层的入选阈值：余弦相似度低于该值的工具不参与补齐。
# 0.20 在全量目录上经验标定：保留「服务响应变慢→probe_http」这类跨词泛化，
# 过滤「随便看看→任意状态类工具」这类只有单个通用词重叠的弱信号。
SEMANTIC_THRESHOLD = 0.20

_VALID_ROLES = ("viewer", "operator", "admin")


@dataclass(frozen=True)
class ToolSpec:
    """单个工具的治理元数据。

    read_only / risk_level / required_role 是变更安全闭环（P1）的判定输入：
    - read_only=True 且 risk_level=0：纯只读，任何角色可调；
    - risk_level=1：低危变更（如重启实例），需 operator 审批；
    - risk_level=2：高危变更（如扩缩容、删数据），需 admin 审批。
    当前目录内全部工具均为只读；写操作工具接入时必须在注册时声明风险等级。

    多语言（C4）：
    - `keywords_en` / `description_en` 为可选英文版本；未填充则 fallback 到
      默认 `keywords` / `description`；profile 的 `locale` 决定选用哪一版。
    """

    name: str
    category: str
    read_only: bool = True
    risk_level: int = 0
    required_role: str = "viewer"
    keywords: tuple[str, ...] = ()
    description: str = ""
    keywords_en: tuple[str, ...] = ()
    description_en: str = ""

    def __post_init__(self) -> None:
        if self.risk_level > 0 and self.read_only:
            raise ValueError(f"工具 {self.name} 声明了风险等级却标记为只读")
        if self.required_role not in _VALID_ROLES:
            raise ValueError(f"工具 {self.name} 的 required_role 非法: {self.required_role}")

    def localized_keywords(self, locale: str) -> tuple[str, ...]:
        """按 locale 返回关键词集合；`en` → 英文，`bilingual` → 中英并集，其它 → 中文。"""
        loc = (locale or "").strip().lower()
        if loc == "en":
            return self.keywords_en or self.keywords
        if loc in {"bilingual", "both"}:
            merged: list[str] = list(self.keywords)
            for keyword in self.keywords_en:
                if keyword not in merged:
                    merged.append(keyword)
            return tuple(merged)
        return self.keywords

    def localized_description(self, locale: str) -> str:
        loc = (locale or "").strip().lower()
        if loc == "en" and self.description_en:
            return self.description_en
        if loc in {"bilingual", "both"} and self.description_en:
            return f"{self.description} / {self.description_en}"
        return self.description


def _spec(
    name: str,
    category: str,
    description: str,
    keywords: tuple[str, ...] = (),
    read_only: bool = True,
    risk_level: int = 0,
    required_role: str = "viewer",
    keywords_en: tuple[str, ...] = (),
    description_en: str = "",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        category=category,
        description=description,
        keywords=keywords,
        read_only=read_only,
        risk_level=risk_level,
        required_role=required_role,
        keywords_en=keywords_en,
        description_en=description_en,
    )


# 全量工具目录：36 MCP（CLS 5 / Monitor 2 / Ops 29）+ 3 本地工具。
TOOL_CATALOG: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        # ---- 本地工具 ----
        _spec(
            "retrieve_knowledge",
            "knowledge",
            "从内部运维知识库检索 runbook、排障手册、历史事件复盘等经验文档",
            ("知识库", "runbook", "手册", "方案", "怎么处理", "如何排查", "经验"),
        ),
        _spec("get_current_time", "time", "获取当前时间", ("时间", "现在")),
        # ---- CLS 日志 ----
        _spec("get_current_timestamp", "logs", "获取当前毫秒级时间戳", ("时间戳",)),
        _spec("get_region_code_by_name", "logs", "按地域名称查询日志服务地域编码", ("地域", "region")),
        _spec(
            "get_topic_info_by_name",
            "logs",
            "按日志主题名称查询主题信息（topic_id 等）",
            ("日志主题", "topic"),
        ),
        _spec(
            "search_topic_by_service_name",
            "logs",
            "按服务名称检索日志主题，定位服务对应的日志存储位置",
            ("日志主题", "服务名", "topic", "cls"),
        ),
        _spec(
            "search_log",
            "logs",
            "检索日志主题中的日志内容，定位错误日志、异常堆栈与告警时段的日志证据",
            ("日志", "log", "报错", "异常栈", "cls"),
        ),
        # ---- Monitor 指标 ----
        _spec(
            "query_cpu_metrics",
            "metrics",
            "查询主机或服务在时间区间内的 CPU 使用率曲线与告警阈值",
            ("cpu", "指标", "监控"),
        ),
        _spec(
            "query_memory_metrics",
            "metrics",
            "查询主机或服务在时间区间内的内存使用率曲线与压力水位",
            ("内存", "memory", "指标", "监控"),
        ),
        # ---- Prometheus ----
        _spec(
            "prom_query",
            "prometheus",
            "执行 PromQL 即时查询，获取指标当前值（QPS、错误率、延迟、饱和度等）",
            ("promql", "指标", "监控", "prometheus"),
        ),
        _spec(
            "prom_query_range",
            "prometheus",
            "执行 PromQL 区间查询，获取指标随时间变化的曲线与趋势，用于判断恶化或恢复",
            ("趋势", "区间", "曲线", "prometheus"),
        ),
        _spec(
            "prom_active_alerts",
            "prometheus",
            "列出 Prometheus 活跃告警及其级别、对象与持续时间",
            ("告警", "alert", "prometheus"),
        ),
        _spec(
            "prom_target_health",
            "prometheus",
            "查询 Prometheus 采集目标健康状态，排查监控采集缺口",
            ("target", "采集目标", "prometheus"),
        ),
        # ---- Kubernetes ----
        _spec(
            "k8s_list_pods",
            "kubernetes",
            "列出 Kubernetes 命名空间下的 Pod 及其运行状态、重启次数",
            ("pod", "k8s", "kubernetes"),
        ),
        _spec(
            "k8s_describe_workload",
            "kubernetes",
            "查看 Kubernetes Deployment/StatefulSet/DaemonSet 工作负载详情与副本状态",
            ("deployment", "workload", "k8s", "kubernetes"),
        ),
        _spec(
            "k8s_get_events",
            "kubernetes",
            "查询 Kubernetes 事件，定位调度失败、镜像拉取失败、OOM 等问题",
            ("event", "事件", "k8s", "kubernetes"),
        ),
        _spec(
            "k8s_get_logs",
            "kubernetes",
            "读取 Kubernetes Pod 容器日志，定位应用报错",
            ("容器日志", "pod日志", "k8s", "kubernetes"),
        ),
        _spec(
            "k8s_rollout_status",
            "kubernetes",
            "查询 Kubernetes 发布滚动状态，判断发布是否卡住或回滚",
            ("rollout", "发布", "k8s", "kubernetes"),
        ),
        # ---- Docker ----
        _spec(
            "docker_list_containers",
            "docker",
            "列出 Docker 容器及运行状态",
            ("docker", "容器"),
        ),
        _spec(
            "docker_inspect_container",
            "docker",
            "查看 Docker 容器配置详情（已剔除环境变量等敏感信息）",
            ("容器详情", "inspect", "docker"),
        ),
        _spec(
            "docker_container_stats",
            "docker",
            "查询 Docker 容器 CPU、内存、网络实时资源占用",
            ("容器资源", "stats", "docker"),
        ),
        # ---- 网络诊断 ----
        _spec(
            "probe_http",
            "network",
            "对允许清单内主机发起只读 HTTP/HEAD 探测，检查服务连通性、响应状态码与延迟",
            ("http", "探测", "连通", "响应"),
        ),
        _spec(
            "resolve_dns",
            "network",
            "解析域名 DNS 记录，排查解析失败或解析到异常地址",
            ("dns", "解析", "域名"),
        ),
        _spec(
            "inspect_tls_certificate",
            "network",
            "检查主机 TLS 证书有效期与证书链，排查证书过期导致的访问失败",
            ("tls", "证书", "https"),
        ),
        # ---- Redis ----
        _spec(
            "redis_info",
            "redis",
            "查询 Redis INFO 安全分区（内存、客户端、持久化等），不含键值数据",
            ("redis", "keyspace", "缓存"),
        ),
        _spec(
            "redis_slowlog",
            "redis",
            "查询 Redis 慢查询日志（参数已脱敏为命令名），定位慢命令",
            ("redis", "slowlog", "慢查询", "缓存"),
        ),
        # ---- MySQL ----
        _spec(
            "mysql_list_tables",
            "mysql",
            "列出 MySQL 白名单 schema 中的数据表",
            ("mysql", "数据库", "表"),
        ),
        _spec(
            "mysql_describe_table",
            "mysql",
            "查看 MySQL 表结构（字段、索引），用于分析表设计",
            ("mysql", "表结构", "数据库"),
        ),
        _spec(
            "mysql_read_query",
            "mysql",
            "执行只读 SELECT/SHOW/DESCRIBE/EXPLAIN 查询（语法白名单，自动限制返回行数），"
            "分析慢查询、锁等待、连接数等数据库问题",
            ("mysql", "sql", "数据库", "锁", "慢查询"),
        ),
        # ---- Loki 日志（真实数据源适配器，P1.4；LOKI_URL 未配置时明确不可用）----
        _spec(
            "loki_query",
            "logs",
            "执行 Loki LogQL 日志查询，检索真实日志内容与错误堆栈（时间窗内）",
            ("loki", "日志", "logql", "日志检索"),
        ),
        _spec(
            "loki_labels",
            "logs",
            "列出 Loki 日志标签名或取值，用于构造 LogQL 查询",
            ("loki", "标签", "label"),
        ),
        _spec(
            "analyze_topology",
            "topology",
            "分析服务的依赖拓扑与故障影响面：直接上游、传递上游爆炸半径（按跳数分层）、依赖链",
            ("拓扑", "影响", "依赖", "上游", "下游", "topology", "爆炸半径"),
        ),
        # ---- 联网检索 ----
        _spec(
            "web_search",
            "web",
            "联网搜索最新资料、官方文档与已知问题（数据会外发到搜索服务）",
            ("联网", "互联网", "最新", "搜索网页"),
            required_role="operator",
        ),
    )
}


# 领域关键词 → 工具名前缀组：命中任一关键词时按确定性路由选择。
# P0-3 起首选值来自 `profiles/<active>.yaml` 的 `tool_groups`；本常量作为
# profile 缺失/损坏时的兜底，改动时请同步 `profiles/aiops.yaml`。
TOOL_GROUPS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("知识库", "runbook", "手册", "方案", "怎么处理", "如何排查"),
        ("retrieve_knowledge",),
    ),
    (
        ("日志", "log", "报错", "异常栈", "cls"),
        ("search_log", "search_topic_by_service_name", "get_topic_info_by_name"),
    ),
    (
        ("cpu", "内存", "memory", "指标", "监控"),
        ("query_cpu_metrics", "query_memory_metrics", "prom_"),
    ),
    (
        ("mysql", "sql", "数据库", "表", "锁", "慢查询"),
        ("mysql_",),
    ),
    (
        ("k8s", "kubernetes", "pod", "deployment", "容器编排"),
        ("k8s_",),
    ),
    (
        ("docker", "容器"),
        ("docker_",),
    ),
    (
        ("redis", "缓存", "slowlog"),
        ("redis_",),
    ),
    (
        ("dns", "网络", "证书", "tls", "http", "连通"),
        ("resolve_dns", "inspect_tls_certificate", "probe_http"),
    ),
    (
        ("联网", "互联网", "最新", "搜索网页"),
        ("web_search",),
    ),
)

# 无任何领域信号时的兜底工具（保持与旧版 tool_router 一致）。
# P0-3 起同样以 profile.default_tool_prefixes 为首选值，本常量兜底。
DEFAULT_TOOL_PREFIXES = (
    "retrieve_knowledge",
    "get_current_time",
    "search_log",
    "query_cpu_metrics",
    "query_memory_metrics",
    "prom_active_alerts",
    "web_search",
)


def _integration_tool_groups() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """Routing hints contributed by enabled optional integrations.

    Imported lazily and defensively: the core must route correctly even when no
    integration is enabled or one of them fails to import.
    """
    try:
        from integrations.loader import extra_tool_groups

        return extra_tool_groups()
    except Exception:  # noqa: BLE001 - integrations must never break routing
        return ()


def _active_tool_groups() -> tuple[tuple[tuple[str, ...], tuple[str, ...]], ...]:
    """返回当前 profile 的 tool_groups；profile 未配置则回落到常量。

    启用的可选集成会把自己的路由关键词追加在后面，因此核心目录里不需要出现
    任何具体厂商的工具前缀。
    """
    try:
        from app.agent.profiles import get_active_profile

        profile = get_active_profile()
    except Exception:  # noqa: BLE001 - profile 层若出错必须不影响路由
        return TOOL_GROUPS + _integration_tool_groups()
    if not profile.has_tool_groups():
        return TOOL_GROUPS + _integration_tool_groups()
    return (
        tuple((group.keywords, group.prefixes) for group in profile.tool_groups)
        + _integration_tool_groups()
    )


def _active_default_prefixes() -> tuple[str, ...]:
    """返回当前 profile 的 default_tool_prefixes；未配置则兜底常量。"""
    try:
        from app.agent.profiles import get_active_profile

        profile = get_active_profile()
    except Exception:  # noqa: BLE001
        return DEFAULT_TOOL_PREFIXES
    if not profile.has_default_tool_prefixes():
        return DEFAULT_TOOL_PREFIXES
    return profile.default_tool_prefixes


def _active_tool_locale() -> str:
    """当前 profile 的工具本地化模式（C4）；解析失败或未配置时返回空串（= 中文）。"""
    try:
        from app.agent.profiles import get_active_profile

        return (get_active_profile().tool_locale or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""

# 未注册工具的保守缺省：按「非只读、需 operator」处理，最小权限原则。
UNKNOWN_TOOL_SPEC = ToolSpec(
    name="<unknown>",
    category="unknown",
    read_only=False,
    risk_level=0,
    required_role="operator",
)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


def _tool_description(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("description", ""))
    return str(getattr(tool, "description", "") or "")


class ToolRegistry:
    """工具注册中心：元数据查询 + 确定性/语义双模路由。"""

    def __init__(self, catalog: dict[str, ToolSpec] | None = None):
        self._catalog: dict[str, ToolSpec] = dict(catalog or TOOL_CATALOG)
        self._semantic_cache_key: tuple[tuple[str, str], ...] | None = None
        self._semantic_cache_index: TfidfIndex | None = None

    # ---- 元数据 ----

    def register(self, spec: ToolSpec) -> None:
        """注册或更新一个工具的治理元数据（第三方 MCP 工具接入时使用）。"""
        self._catalog[spec.name] = spec
        self._semantic_cache_key = None
        self._semantic_cache_index = None

    def get(self, name: str) -> ToolSpec | None:
        return self._catalog.get(name)

    def spec_for(self, name: str) -> ToolSpec:
        """返回工具元数据；未注册工具返回保守缺省（非只读、operator 起步）。"""
        return self._catalog.get(name) or ToolSpec(
            name=name,
            category=UNKNOWN_TOOL_SPEC.category,
            read_only=UNKNOWN_TOOL_SPEC.read_only,
            risk_level=UNKNOWN_TOOL_SPEC.risk_level,
            required_role=UNKNOWN_TOOL_SPEC.required_role,
        )

    @property
    def catalog(self) -> dict[str, ToolSpec]:
        return dict(self._catalog)

    def read_only_tools(self) -> list[str]:
        return [name for name, spec in self._catalog.items() if spec.read_only]

    def risky_tools(self) -> list[str]:
        return [name for name, spec in self._catalog.items() if spec.risk_level > 0]

    # ---- 路由 ----

    def _domain_prefixes(self, normalized_question: str) -> list[str]:
        """问题命中的领域关键词对应的工具名/前缀（不含兜底默认工具）。"""
        locale = _active_tool_locale()
        prefixes: list[str] = []
        for name, spec in self._catalog.items():
            keywords = spec.localized_keywords(locale) if locale else spec.keywords
            if any(
                str(keyword).casefold() in normalized_question
                for keyword in keywords
            ):
                prefixes.append(name)
        for keywords, tool_prefixes in _active_tool_groups():
            if any(keyword.casefold() in normalized_question for keyword in keywords):
                prefixes.extend(tool_prefixes)
        return prefixes

    def _select_by_prefixes(
        self, prefixes: list[str], tools: list[Any], limit: int
    ) -> list[Any]:
        selected: list[Any] = []
        selected_names: set[str] = set()
        for prefix in prefixes:
            for tool in tools:
                name = _tool_name(tool)
                if name and name not in selected_names and (
                    name == prefix or name.startswith(prefix)
                ):
                    selected.append(tool)
                    selected_names.add(name)
                    if len(selected) >= limit:
                        return selected
        return selected

    def _routing_text(self, tool: Any) -> str:
        """参与语义索引的文本：工具名 + 运行时描述 + 注册元数据。"""
        locale = _active_tool_locale()
        name = _tool_name(tool)
        spec = self._catalog.get(name)
        parts = [name, _tool_description(tool)]
        if spec:
            description = (
                spec.localized_description(locale) if locale else spec.description
            )
            keywords = spec.localized_keywords(locale) if locale else spec.keywords
            parts.extend((spec.category, description, " ".join(keywords)))
        return " ".join(part for part in parts if part)

    def _semantic_rank(self, question: str, tools: list[Any]) -> list[tuple[Any, float]]:
        documents = [self._routing_text(tool) for tool in tools]
        cache_key = tuple(zip(map(_tool_name, tools), documents, strict=True))
        if cache_key != self._semantic_cache_key or self._semantic_cache_index is None:
            self._semantic_cache_index = TfidfIndex(documents)
            self._semantic_cache_key = cache_key
        scores = self._semantic_cache_index.score(question)
        # TfidfIndex.score returns exactly one score per indexed document, so a
        # length mismatch here means the cache and the index disagree.
        ranked = sorted(zip(tools, scores, strict=True), key=lambda pair: -pair[1])
        return ranked

    def select(self, question: str, tools: list[Any], limit: int) -> list[Any]:
        """双模路由：领域关键词命中 → 确定性路由；未命中 → 语义泛化补齐。"""
        if not tools or limit <= 0:
            return []

        normalized = question.casefold()
        domain_prefixes = self._domain_prefixes(normalized)
        default_prefixes = list(_active_default_prefixes())
        if domain_prefixes:
            # 有关键词信号：保持确定性路由行为（与旧版一致，含空选兜底）
            return self._select_by_prefixes(
                domain_prefixes + default_prefixes, tools, limit
            ) or tools[:limit]

        # 无领域信号：默认工具兜底 + 语义相似度补齐
        selected = self._select_by_prefixes(default_prefixes, tools, limit)
        if len(selected) >= limit:
            return selected
        selected_names = {_tool_name(tool) for tool in selected}
        for tool, score in self._semantic_rank(question, tools):
            if score < SEMANTIC_THRESHOLD:
                break
            name = _tool_name(tool)
            if name and name not in selected_names:
                selected.append(tool)
                selected_names.add(name)
                if len(selected) >= limit:
                    break
        return selected or tools[:limit]


# 全局注册中心实例（tests 通过 tool_registry.tool_registry 访问/重置）
tool_registry = ToolRegistry()
