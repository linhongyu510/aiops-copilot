"""拓扑影响面分析工具：把服务依赖图暴露给 Agent 排障使用"""

from typing import Any

from langchain_core.tools import tool
from loguru import logger

from app.observability import tool_metrics
from app.services.topology_service import get_topology_service


def _format_impact(result: dict[str, Any]) -> str:
    if not result.get("available"):
        return f"拓扑服务不可用：{result.get('reason', 'unknown')}"
    if not result.get("found"):
        known = ", ".join(result.get("known_services", []))
        return (
            f"拓扑中未找到服务 {result.get('service')}。"
            f"已知服务：{known}。请确认服务名后重试。"
        )
    lines = [
        f"服务: {result['service']}",
        f"元信息: {result.get('meta', {})}",
        f"直接上游（依赖该服务的服务）: {', '.join(result['direct_dependents']) or '无'}",
    ]
    blast = result.get("blast_radius", {})
    if blast:
        rendered = "；".join(
            f"{depth} 跳: {', '.join(nodes)}" for depth, nodes in blast.items()
        )
        lines.append(f"受影响范围（传递上游，共 {result['impacted_service_count']} 个）: {rendered}")
    else:
        lines.append("受影响范围: 无上游依赖方")
    lines.append(
        f"直接依赖（该服务依赖的组件）: {', '.join(result['direct_dependencies']) or '无'}"
    )
    chain = result.get("dependency_chain", {})
    if chain:
        rendered = "；".join(
            f"{depth} 跳: {', '.join(nodes)}" for depth, nodes in chain.items()
        )
        lines.append(f"依赖链: {rendered}")
    lines.append(f"数据来源: {result.get('source', '')}（如为 builtin-demo 则为演示拓扑）")
    return "\n".join(lines)


@tool
async def analyze_topology(service_name: str) -> str:
    """分析服务的依赖拓扑与故障影响面。

    给定服务名，返回：直接上游（谁依赖它）、受影响范围（传递上游/爆炸半径，
    按跳数分层）、直接依赖与依赖链。用于评估故障影响面与排障时判断
    「这个问题会影响哪些业务」。

    Args:
        service_name: 服务名，如 elasticsearch、mysql、order-service

    Returns:
        str: 人类可读的影响面分析
    """
    import time as _time

    started = _time.perf_counter()
    try:
        result = get_topology_service().impact_analysis(service_name)
        text = _format_impact(result)
        tool_metrics.record(
            "analyze_topology",
            success=True,
            latency_ms=(_time.perf_counter() - started) * 1000,
            arguments={"service_name": service_name},
        )
        return text
    except Exception as exc:
        logger.error(f"拓扑分析失败: {exc}")
        tool_metrics.record(
            "analyze_topology",
            success=False,
            latency_ms=(_time.perf_counter() - started) * 1000,
            arguments={"service_name": service_name},
        )
        return f"拓扑分析失败: {exc}"
