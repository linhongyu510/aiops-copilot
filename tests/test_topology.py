"""P2.2：服务拓扑与影响面分析"""

import json

import pytest

from app.services.topology_service import (
    TopologyService,
)


@pytest.fixture
def service():
    return TopologyService()  # 无路径 → 内置演示拓扑


def test_builtin_demo_topology_loads_with_source_label(service):
    result = service.impact_analysis("elasticsearch")
    assert result["available"] is True
    assert result["source"] == "builtin-demo"  # 演示数据显式标注，不冒充 CMDB


def test_blast_radius_is_transitive_and_layered(service):
    """ES 故障：1 跳影响 search-service，2 跳影响 api-gateway，3 跳 web-console"""
    result = service.impact_analysis("elasticsearch")
    assert result["found"] is True
    assert result["direct_dependents"] == ["search-service"]
    assert result["blast_radius"]["hop_1"] == ["search-service"]
    assert result["blast_radius"]["hop_2"] == ["api-gateway"]
    assert result["blast_radius"]["hop_3"] == ["web-console"]
    assert result["impacted_service_count"] == 3


def test_dependency_chain_follows_forward_edges(service):
    """api-gateway 依赖链：order/search → payment/mysql/kafka/es..."""
    result = service.impact_analysis("api-gateway")
    assert "order-service" in result["direct_dependencies"]
    assert "search-service" in result["direct_dependencies"]
    hop2 = set(result["dependency_chain"]["hop_2"])
    assert {"payment-service", "mysql", "kafka", "elasticsearch"}.issubset(hop2)


def test_edge_service_has_no_dependents(service):
    result = service.impact_analysis("mysql")
    # mysql 是最底层：无下游依赖
    assert result["direct_dependencies"] == []
    assert result["dependency_chain"] == {}
    # 但上游爆炸半径很大（order/payment/data-sync/elasticsearch...）
    assert result["impacted_service_count"] >= 4


def test_alias_matching_and_unknown_service(service):
    # 实例名容错：mysql-0 → mysql
    result = service.impact_analysis("MySQL-0")
    assert result["found"] is True
    assert result["service"] == "mysql"
    # 完全未知的服务返回已知清单
    missing = service.impact_analysis("nonexistent-service")
    assert missing["found"] is False
    assert "elasticsearch" in missing["known_services"]


def test_configured_but_broken_path_reports_unavailable(tmp_path):
    broken = TopologyService(topology_path=str(tmp_path / "missing.json"))
    result = broken.impact_analysis("mysql")
    assert result["available"] is False
    assert result["reason"] == "topology_not_configured"


def test_custom_topology_from_file(tmp_path):
    graph = {
        "services": {"a": {"category": "x"}, "b": {}, "c": {}},
        "dependencies": [["a", "b"], ["b", "c"]],
    }
    path = tmp_path / "topo.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    service = TopologyService(topology_path=str(path))
    result = service.impact_analysis("c")
    assert result["source"] == "file:topo.json"
    assert result["blast_radius"]["hop_1"] == ["b"]
    assert result["blast_radius"]["hop_2"] == ["a"]


def test_reload_picks_up_file_changes(tmp_path):
    path = tmp_path / "topo.json"
    path.write_text(json.dumps({"services": {"a": {}, "b": {}}, "dependencies": []}), encoding="utf-8")
    service = TopologyService(topology_path=str(path))
    assert service.impact_analysis("b")["found"] is True

    path.write_text(
        json.dumps({"services": {"a": {}, "new-svc": {}}, "dependencies": []}),
        encoding="utf-8",
    )
    service.reload()
    assert service.impact_analysis("new-svc")["found"] is True
    assert service.impact_analysis("b")["found"] is False


# ---- 工具封装 ----


async def test_analyze_topology_tool_formats_impact():
    from app.tools.topology_tool import analyze_topology

    text = await analyze_topology.ainvoke({"service_name": "elasticsearch"})
    assert "elasticsearch" in text
    assert "search-service" in text
    assert "api-gateway" in text
    assert "builtin-demo" in text  # 来源标注透传


async def test_analyze_topology_tool_unknown_service():
    from app.tools.topology_tool import analyze_topology

    text = await analyze_topology.ainvoke({"service_name": "no-such-thing"})
    assert "未找到服务" in text


def test_tool_registered_in_catalog():
    from app.agent.tool_registry import TOOL_CATALOG

    assert "analyze_topology" in TOOL_CATALOG
    # 拓扑关键词命中确定性路由
    from types import SimpleNamespace

    from app.agent.tool_router import select_relevant_tools

    tools = [SimpleNamespace(name=n, description=n) for n in ("analyze_topology", "web_search")]
    selected = select_relevant_tools("评估一下影响面和上游依赖", tools, limit=2)
    assert "analyze_topology" in [t.name for t in selected]
