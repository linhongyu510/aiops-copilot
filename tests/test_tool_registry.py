"""工具注册中心（P0.1）：元数据治理 + 确定性/语义双模路由"""

from types import SimpleNamespace

import pytest

from app.agent.tool_registry import (
    TOOL_CATALOG,
    ToolRegistry,
    ToolSpec,
    tool_registry,
)

ALL_NAMES = list(TOOL_CATALOG.keys())


def _named_tools(*names: str, description_template: str = "{name} 的描述") -> list:
    return [
        SimpleNamespace(name=name, description=description_template.format(name=name))
        for name in names
    ]


# ---- 元数据治理 ----


def test_catalog_covers_full_tool_contract():
    """目录覆盖全部 36 个工具契约（34 MCP + 2 本地），且均为只读零风险"""
    assert len(TOOL_CATALOG) == 39
    assert all(spec.read_only for spec in TOOL_CATALOG.values())
    assert all(spec.risk_level == 0 for spec in TOOL_CATALOG.values())
    for required in ("retrieve_knowledge", "get_current_time", "web_search"):
        assert required in TOOL_CATALOG


def test_spec_validation_rejects_risky_readonly_and_bad_role():
    with pytest.raises(ValueError):
        ToolSpec(name="bad", category="x", read_only=True, risk_level=1)
    with pytest.raises(ValueError):
        ToolSpec(name="bad", category="x", required_role="root")


def test_spec_for_unknown_tool_is_conservative():
    spec = tool_registry.spec_for("third_party_write_tool")
    assert spec.read_only is False
    assert spec.required_role == "operator"


def test_register_updates_metadata_and_refreshes_semantic_cache():
    registry = ToolRegistry()
    tools = _named_tools("retrieve_knowledge", "acme_deploy")
    # 未注册时：语义层只能看到无语义的工具名，无法命中
    selected = registry.select("发布新版本到生产环境", tools, limit=2)
    assert "acme_deploy" not in [t.name for t in selected]
    registry.register(
        ToolSpec(
            name="acme_deploy",
            category="change",
            description="发布新版本到生产环境，执行滚动发布",
            keywords=("发布",),
        )
    )
    selected = registry.select("帮忙发布新版本到生产环境", tools, limit=2)
    assert "acme_deploy" in [t.name for t in selected]


def test_risky_tools_tracklist_for_action_governor():
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="k8s_rollout_restart",
            category="kubernetes",
            read_only=False,
            risk_level=1,
            required_role="operator",
            description="重启工作负载（低危变更）",
        )
    )
    assert registry.risky_tools() == ["k8s_rollout_restart"]
    assert registry.read_only_tools() and "k8s_rollout_restart" not in registry.read_only_tools()


# ---- 双模路由 ----


def test_keyword_hit_keeps_deterministic_routing():
    """领域关键词命中：确定性路由选中相关领域工具，无关领域不混入"""
    tools = _named_tools(*ALL_NAMES)
    selected = tool_registry.select("数据库出现慢查询，请分析", tools, limit=12)
    names = [t.name for t in selected]
    assert "mysql_read_query" in names
    assert "mysql_list_tables" in names
    assert "windos_recent_audit" not in names
    assert "docker_list_containers" not in names


def test_semantic_generalization_without_domain_keyword():
    """无领域关键词的模糊问法：语义层补齐相关工具（泛化能力核心用例）"""
    tools = _named_tools(*ALL_NAMES)
    selected = tool_registry.select("服务响应变慢了，帮忙看看", tools, limit=12)
    names = [t.name for t in selected]
    assert "probe_http" in names
    # 兜底工具仍在
    assert "retrieve_knowledge" in names
    assert "get_current_time" in names
    # 无关领域工具不因泛化混入
    assert "mysql_list_tables" not in names
    assert "windos_recent_audit" not in names


def test_semantic_layer_respects_threshold():
    """低于阈值的弱信号（单个通用词重叠）不入选"""
    tools = _named_tools(*ALL_NAMES)
    selected = tool_registry.select("随便看看情况", tools, limit=12)
    names = [t.name for t in selected]
    assert "windos_queue_status" not in names
    assert "mysql_read_query" not in names


def test_select_limit_and_empty_inputs():
    tools = _named_tools(*ALL_NAMES)
    assert len(tool_registry.select("看看 k8s pod 状态", tools, limit=3)) == 3
    assert tool_registry.select("任意问题", [], limit=8) == []
    assert tool_registry.select("任意问题", tools, limit=0) == []


def test_dict_shaped_tools_are_supported():
    tools = [
        {"name": "k8s_list_pods", "description": "列出 Pod"},
        {"name": "mysql_read_query", "description": "只读查询"},
    ]
    selected = tool_registry.select("k8s pod 一直在重启", tools, limit=2)
    assert [t["name"] for t in selected] == ["k8s_list_pods"]


def test_description_text_participates_in_semantic_index():
    """运行时 description（真实 MCP 工具带详细描述）参与语义匹配"""
    tools = [
        SimpleNamespace(name="custom_cache_tool", description="查询缓存命中率与过期策略"),
        SimpleNamespace(name="unrelated_tool", description="查看工单列表"),
    ]
    selected = tool_registry.select("命中率跌得很厉害", tools, limit=2)
    assert [t.name for t in selected] == ["custom_cache_tool"]


def test_keyword_hit_with_absent_tools_falls_back_to_discovery_order():
    """关键词命中的工具不在运行时列表时，回退为按发现顺序取前 N 个（旧版行为）"""
    tools = [
        SimpleNamespace(name="custom_cache_tool", description="查询缓存命中率与过期策略"),
        SimpleNamespace(name="unrelated_tool", description="查看工单列表"),
    ]
    selected = tool_registry.select("缓存过期太频繁了怎么办", tools, limit=2)
    assert [t.name for t in selected] == ["custom_cache_tool", "unrelated_tool"]


# ------------------- P1-2 C4：ToolSpec 关键词/描述本地化 -------------------


def test_c4_localized_keywords_fallback_to_default_when_english_missing():
    """C4：locale=en 但 spec 未提供英文关键词时，回落到默认中文关键词。"""
    spec = ToolSpec(
        name="test",
        category="x",
        keywords=("日志", "报错"),
        description="日志检索工具",
    )
    assert spec.localized_keywords("en") == ("日志", "报错")
    assert spec.localized_description("en") == "日志检索工具"


def test_c4_localized_keywords_returns_english_when_provided():
    """C4：locale=en 且 spec 提供英文关键词时使用英文版。"""
    spec = ToolSpec(
        name="test",
        category="x",
        keywords=("日志",),
        description="日志",
        keywords_en=("log", "error"),
        description_en="Log search tool",
    )
    assert spec.localized_keywords("en") == ("log", "error")
    assert spec.localized_description("en") == "Log search tool"


def test_c4_localized_keywords_bilingual_merges_both():
    """C4：locale=bilingual 时合并中英关键词并去重。"""
    spec = ToolSpec(
        name="test",
        category="x",
        keywords=("日志", "报错"),
        description="日志",
        keywords_en=("log", "报错"),  # 「报错」故意重复
        description_en="Log",
    )
    merged = spec.localized_keywords("bilingual")
    assert "日志" in merged and "log" in merged and "报错" in merged
    # 去重
    assert merged.count("报错") == 1
    assert "/" in spec.localized_description("bilingual")


def test_c4_localized_keywords_unknown_locale_returns_default():
    """C4：未识别 locale 一律回落到默认（中文）。"""
    spec = ToolSpec(
        name="t",
        category="x",
        keywords=("日志",),
        keywords_en=("log",),
    )
    assert spec.localized_keywords("fr") == ("日志",)
    assert spec.localized_keywords("") == ("日志",)
