"""预案库（Procedural Memory，P2.4）：从「LLM 现场推理」到「预案匹配 + 验证」。

三层记忆的第三层：
- Episodic（P0.3 事件记忆）：自动沉淀、相似检索——「上次发生了什么」；
- Semantic（P2.2 拓扑）：服务依赖与影响面——「组件在哪里」；
- Procedural（本模块）：人工评审过的可复用排障预案——「标准怎么做」。

设计：
- 预案来源：AIOPS_PLAYBOOKS_PATH 指定的 JSONL（真实运维沉淀，
  含 review_status 字段）；未配置时使用内置演示预案并显式标注
  source="builtin-demo"，与拓扑的诚实标注约定一致；
- 匹配：n-gram TF-IDF 余弦（title+symptoms+root_cause_hint），
  阈值过滤，确定性可离线测试；
- 注入：Planner 在 runbook 检索与事件记忆之外追加「匹配预案」上下文，
  预案步骤直接参考但必须用工具验证前提条件；
- API：GET /api/playbooks 列出、GET /api/playbooks/match 调试匹配。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from app.agent.text_similarity import TfidfIndex
from app.config import config

# 内置演示预案：与 aiops-docs 语料场景对齐（ES red / Kafka lag / 连接池耗尽）
BUILTIN_DEMO_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "playbook_id": "pb-demo-es-red",
        "title": "Elasticsearch 集群状态 red 排障预案",
        "symptoms": ["es red", "集群状态 red", "分片未分配", "unassigned_shards", "搜索超时"],
        "steps": [
            {"description": "查询集群健康与未分配分片数", "tool_hint": "prom_query"},
            {"description": "检查磁盘水位是否触发 flood-stage 只读锁", "tool_hint": "prom_query"},
            {"description": "查看 master/elasticsearch 日志中的分片分配失败原因", "tool_hint": "loki_query"},
            {"description": "评估影响面：哪些上游依赖搜索服务", "tool_hint": "analyze_topology"},
        ],
        "root_cause_hint": "常见根因：磁盘超水位 flood-stage、节点离线导致副本不足、分片分配失败",
        "remediation": "解除只读锁前先清理磁盘或扩容；副本不足时等待重分配或临时降低副本数（变更需审批）",
        "verify": ["cluster_health.status == green/yellow", "搜索接口 P99 恢复基线"],
        "review_status": "demo",
    },
    {
        "playbook_id": "pb-demo-kafka-lag",
        "title": "Kafka 消费组积压（consumer lag）排障预案",
        "symptoms": ["kafka lag", "消费积压", "consumer group lag", "消息堆积", "处理延迟上升"],
        "steps": [
            {"description": "查询消费组 lag 趋势，确认是突增还是缓增", "tool_hint": "prom_query_range"},
            {"description": "对比生产速率与消费速率，判断上游流量突增还是消费变慢", "tool_hint": "prom_query"},
            {"description": "检查消费者日志是否有 rebalance/超时/异常", "tool_hint": "loki_query"},
            {"description": "检查消费组是否触发 ISR 收缩或 broker 异常", "tool_hint": "prom_active_alerts"},
        ],
        "root_cause_hint": "常见根因：上游流量突增、消费者处理变慢（下游依赖慢）、rebalance 风暴",
        "remediation": "下游慢则优化或扩容消费者（变更需审批）；rebalance 风暴检查 session/心跳配置",
        "verify": ["lag 曲线回落到基线", "消费速率 ≥ 生产速率"],
        "review_status": "demo",
    },
    {
        "playbook_id": "pb-demo-db-pool",
        "title": "数据库连接池耗尽排障预案",
        "symptoms": ["连接池耗尽", "connection pool exhausted", "too many connections", "获取连接超时", "数据库报错"],
        "steps": [
            {"description": "查询当前连接数与最大连接数", "tool_hint": "mysql_read_query"},
            {"description": "找出占用连接的会话与长事务", "tool_hint": "mysql_read_query"},
            {"description": "关联应用日志中获取连接超时的时段", "tool_hint": "search_log"},
            {"description": "检查慢查询是否堆积导致连接释放变慢", "tool_hint": "mysql_read_query"},
        ],
        "root_cause_hint": "常见根因：慢查询堆积、连接泄漏、连接池配置过小、突发流量",
        "remediation": "kill 长事务（变更需审批）、优化慢查询、评估调大连接池（变更需审批）",
        "verify": ["活跃连接数回落", "获取连接耗时恢复正常"],
        "review_status": "demo",
    },
]


class PlaybookService:
    """预案库：加载、匹配与上下文注入。"""

    def __init__(self, path: str | None = None):
        self._path = path
        self._loaded = False
        self.source = ""
        self.playbooks: list[dict[str, Any]] = []
        self._index: TfidfIndex | None = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path:
            path = Path(self._path)
            try:
                rows: list[dict[str, Any]] = []
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(f"预案文件存在坏行，已跳过: {path}")
                        continue
                    if isinstance(row, dict) and row.get("playbook_id"):
                        rows.append(row)
                self.playbooks = rows
                self.source = f"file:{path.name}"
            except Exception as exc:
                # 显式配置但不可读：如实空库，不静默换演示预案
                logger.warning(f"预案文件加载失败（{path}），预案库为空: {exc}")
                self.playbooks = []
                self.source = ""
        else:
            self.playbooks = [dict(item) for item in BUILTIN_DEMO_PLAYBOOKS]
            self.source = "builtin-demo"
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        if not self.playbooks:
            self._index = None
            return
        self._index = TfidfIndex([self._playbook_text(item) for item in self.playbooks])

    @staticmethod
    def _playbook_text(playbook: dict[str, Any]) -> str:
        return " ".join(
            [
                str(playbook.get("title", "")),
                " ".join(str(item) for item in playbook.get("symptoms", [])),
                str(playbook.get("root_cause_hint", "")),
            ]
        )

    def reload(self) -> None:
        self._loaded = False
        self.playbooks = []
        self._ensure_loaded()

    # ---- 匹配与注入 ----

    def match(self, query: str, k: int | None = None) -> list[dict[str, Any]]:
        """按相似度返回 top-k 预案（带 score 字段）。"""
        self._ensure_loaded()
        if not self.playbooks or self._index is None or not (query or "").strip():
            return []
        top_k = k or config.aiops_playbook_top_k
        hits = self._index.top_k(query, k=top_k, min_score=config.aiops_playbook_min_score)
        results = []
        for index, score in hits:
            item = dict(self.playbooks[index])
            item["score"] = round(score, 4)
            results.append(item)
        return results

    def format_matched_playbooks(self, query: str, k: int | None = None) -> str:
        """渲染注入 Planner 的「匹配预案」上下文块。"""
        matched = self.match(query, k)
        if not matched:
            return ""
        blocks: list[str] = []
        for playbook in matched:
            lines = [
                f"### 预案: {playbook.get('title', '')}（匹配度 {playbook.get('score', 0)}）",
                f"- 适用症状: {'；'.join(str(s) for s in playbook.get('symptoms', []))}",
            ]
            steps = playbook.get("steps", [])
            if steps:
                rendered = "\n".join(
                    f"  {i}. {step.get('description', '')}"
                    + (f"（建议工具: {step['tool_hint']}）" if step.get("tool_hint") else "")
                    for i, step in enumerate(steps, 1)
                )
                lines.append(f"- 标准步骤:\n{rendered}")
            if playbook.get("root_cause_hint"):
                lines.append(f"- 根因提示: {playbook['root_cause_hint']}")
            if playbook.get("verify"):
                lines.append(
                    f"- 验证标准: {'；'.join(str(v) for v in playbook['verify'])}"
                )
            blocks.append("\n".join(lines))
        return (
            "## 匹配预案\n\n"
            "以下是运维预案库中与当前任务匹配的标准排障预案。"
            "制定计划时优先参考预案步骤，但**必须用工具验证前提条件仍然成立**，"
            "不要未经验证直接套用结论：\n\n" + "\n\n".join(blocks)
        )

    def list_playbooks(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return list(self.playbooks)


# 全局单例（路径来自 config，惰性加载）
playbook_service: PlaybookService | None = None


def get_playbook_service() -> PlaybookService:
    global playbook_service
    if playbook_service is None:
        playbook_service = PlaybookService(
            path=config.aiops_playbooks_path or None
        )
    return playbook_service
