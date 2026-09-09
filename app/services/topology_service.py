"""服务拓扑与影响面分析（P2.2）。

诊断此前缺少「这个组件在系统中的位置」维度：
- ES red 到底影响哪些上游？MySQL 慢查询的业务爆炸半径多大？
- 本模块用有向依赖图（A depends_on B）回答这两类问题。

数据来源（按优先级）：
1. AIOPS_TOPOLOGY_PATH 指定的 JSON 文件（真实环境用 CMDB 导出）；
2. 未配置/不可读时使用内置演示拓扑，并在返回中显式标注
   source="builtin-demo"——可演示、不冒充真实 CMDB。

图规模为运维服务拓扑量级（百级节点），BFS 足够，无需图数据库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

# 内置演示拓扑：与 aiops-docs 语料场景（ES/Kafka/MySQL/Redis 故障）对齐
BUILTIN_DEMO_TOPOLOGY: dict[str, Any] = {
    "services": {
        "api-gateway": {"category": "gateway", "tier": "edge"},
        "web-console": {"category": "gateway", "tier": "edge"},
        "order-service": {"category": "business", "tier": "app"},
        "payment-service": {"category": "business", "tier": "app"},
        "search-service": {"category": "business", "tier": "app"},
        "data-sync-service": {"category": "data", "tier": "app"},
        "notification-service": {"category": "business", "tier": "app"},
        "elasticsearch": {"category": "middleware", "tier": "infra"},
        "kafka": {"category": "middleware", "tier": "infra"},
        "redis": {"category": "middleware", "tier": "infra"},
        "mysql": {"category": "database", "tier": "data"},
    },
    # [A, B] 表示 A 依赖 B
    "dependencies": [
        ["api-gateway", "order-service"],
        ["api-gateway", "search-service"],
        ["web-console", "api-gateway"],
        ["order-service", "payment-service"],
        ["order-service", "mysql"],
        ["order-service", "kafka"],
        ["payment-service", "mysql"],
        ["payment-service", "redis"],
        ["search-service", "elasticsearch"],
        ["data-sync-service", "mysql"],
        ["data-sync-service", "kafka"],
        ["notification-service", "kafka"],
        ["elasticsearch", "mysql"],
    ],
}


class TopologyService:
    """有向依赖图（A depends_on B）与影响面查询。"""

    def __init__(self, topology_path: str | None = None):
        self._topology_path = topology_path
        self._loaded = False
        self.source = ""
        self.services: dict[str, dict[str, Any]] = {}
        self.dependencies: list[list[str]] = []
        self._forward: dict[str, set[str]] = {}  # A -> {B...}（A 依赖 B）
        self._reverse: dict[str, set[str]] = {}  # B -> {A...}（谁依赖 B）

    # ---- 加载 ----

    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return bool(self.services)
        self._loaded = True
        if self._topology_path:
            path = Path(self._topology_path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._apply(data, source=f"file:{path.name}")
                return True
            except Exception as exc:
                # 显式配置了路径但不可读：如实报告不可用，不静默换演示数据
                logger.warning(f"拓扑文件加载失败（{path}）: {exc}")
                return False
        self._apply(BUILTIN_DEMO_TOPOLOGY, source="builtin-demo")
        return True

    def _apply(self, data: dict[str, Any], *, source: str) -> None:
        services = data.get("services", {}) or {}
        deps = data.get("dependencies", []) or []
        if not isinstance(services, dict) or not isinstance(deps, list):
            raise ValueError("拓扑格式非法：需要 services dict 与 dependencies list")
        self.source = source
        self.services = {
            str(name): dict(meta) if isinstance(meta, dict) else {"category": str(meta)}
            for name, meta in services.items()
        }
        self.dependencies = []
        self._forward = {name: set() for name in self.services}
        self._reverse = {name: set() for name in self.services}
        for edge in deps:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                continue
            upstream, downstream = str(edge[0]), str(edge[1])
            if upstream not in self.services or downstream not in self.services:
                continue
            self.dependencies.append([upstream, downstream])
            self._forward[upstream].add(downstream)
            self._reverse[downstream].add(upstream)

    def reload(self) -> None:
        """强制重新加载（拓扑文件更新后调用）。"""
        self._loaded = False
        self.services = {}
        self._ensure_loaded()

    # ---- 查询 ----

    def _bfs_levels(self, start: str, graph: dict[str, set[str]]) -> dict[int, list[str]]:
        """按跳数分层遍历，返回 {depth: [nodes]}（不含 start 自身）。"""
        levels: dict[int, list[str]] = {}
        visited = {start}
        frontier = [start]
        depth = 0
        while frontier:
            depth += 1
            next_frontier: list[str] = []
            for node in frontier:
                for neighbor in sorted(graph.get(node, ())):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    next_frontier.append(neighbor)
            if next_frontier:
                levels[depth] = sorted(next_frontier)
                frontier = next_frontier
            else:
                break
        return levels

    def impact_analysis(self, service_name: str) -> dict[str, Any]:
        """影响面分析：上游爆炸半径（谁受影响）+ 下游依赖（我依赖谁）。"""
        if not self._ensure_loaded():
            return {"available": False, "reason": "topology_not_configured"}
        name = service_name.strip().lower()
        # 别名容错：mysql-0 / mysql 实例名归一
        if name not in self.services:
            for candidate in self.services:
                if candidate in name or name in candidate:
                    name = candidate
                    break
        if name not in self.services:
            return {
                "available": True,
                "source": self.source,
                "service": service_name,
                "found": False,
                "known_services": sorted(self.services),
            }

        upstream_levels = self._bfs_levels(name, self._reverse)
        downstream_levels = self._bfs_levels(name, self._forward)
        return {
            "available": True,
            "source": self.source,
            "found": True,
            "service": name,
            "meta": self.services.get(name, {}),
            "direct_dependents": sorted(self._reverse.get(name, set())),
            # 爆炸半径：全部上游（传递闭包），按跳数分层
            "blast_radius": {
                f"hop_{depth}": nodes for depth, nodes in upstream_levels.items()
            },
            "impacted_service_count": sum(len(v) for v in upstream_levels.values()),
            "direct_dependencies": sorted(self._forward.get(name, set())),
            "dependency_chain": {
                f"hop_{depth}": nodes for depth, nodes in downstream_levels.items()
            },
        }


# 全局单例（路径来自 config，惰性加载）
topology_service: TopologyService | None = None


def get_topology_service() -> TopologyService:
    global topology_service
    if topology_service is None:
        from app.config import config

        topology_service = TopologyService(
            topology_path=config.aiops_topology_path or None
        )
    return topology_service
