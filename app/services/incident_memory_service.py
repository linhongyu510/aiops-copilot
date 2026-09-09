"""事件记忆（Episodic Memory，P0.3）：诊断经验自动沉淀与相似检索。

解决的问题：AIOps 图每次诊断前会重置 thread checkpoint（past_steps 是
append-only reducer），导致「同一个问题发生 100 次，每次都当第一次见」。

设计：
- 每次诊断结束自动沉淀一条结构化 episode（原始任务 / 步骤与结果 /
  报告摘要），JSONL 持久化在 .runtime/，跨进程重启存活；
- Planner 制定计划前按 n-gram TF-IDF 余弦检索相似历史事件，
  注入「历史相似事件」上下文（与 runbook 检索互补）；
- 全链路降级：读取/写入/检索任一失败只记日志，绝不阻断诊断主流程；
- 检索层与工具路由共享 app/agent/text_similarity，后续可平滑
  替换为 embedding 检索。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from app.agent.text_similarity import TfidfIndex
from app.config import config

# 单个 episode 各字段的长度上限，防止长报告把记忆文件撑爆
_MAX_INPUT_CHARS = 600
_MAX_STEP_RESULT_CHARS = 400
_MAX_RESPONSE_CHARS = 800


def _clamp(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class IncidentMemoryService:
    """事件记忆服务：JSONL 持久化 + TF-IDF 相似检索。"""

    def __init__(self, path: str | None = None, max_episodes: int | None = None):
        self._path = Path(path or config.aiops_incident_memory_path)
        self._max_episodes = max_episodes or config.aiops_incident_memory_max_episodes
        self._episodes: list[dict[str, Any]] = []
        self._index: TfidfIndex | None = None
        self._lock = asyncio.Lock()
        self._loaded = False

    # ---- 持久化 ----

    def _load(self) -> None:
        """惰性加载历史 episode；文件损坏时跳过坏行而不是整体失败。"""
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            episode = json.loads(line)
                        except json.JSONDecodeError:
                            logger.warning(f"事件记忆文件存在坏行，已跳过: {self._path}")
                            continue
                        if isinstance(episode, dict) and episode.get("episode_id"):
                            self._episodes.append(episode)
        except Exception as exc:
            logger.warning(f"加载事件记忆失败（记忆检索将退化为空）: {exc}")
        self._rebuild_index()

    def _rotate_if_needed(self) -> None:
        """超过容量上限时淘汰最旧的 episode 并重写文件。"""
        if len(self._episodes) <= self._max_episodes:
            return
        dropped = len(self._episodes) - self._max_episodes
        self._episodes = self._episodes[dropped:]
        logger.info(f"事件记忆超过上限 {self._max_episodes}，淘汰最旧 {dropped} 条")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as handle:
                for episode in self._episodes:
                    handle.write(json.dumps(episode, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning(f"重写事件记忆文件失败: {exc}")

    # ---- 索引 ----

    @staticmethod
    def _episode_text(episode: dict[str, Any]) -> str:
        """参与相似度计算的文本：任务 + 步骤描述 + 根因结论摘要。"""
        parts = [str(episode.get("input", ""))]
        parts.extend(
            str(step.get("description", "")) for step in episode.get("steps", [])
        )
        parts.append(str(episode.get("response_summary", "")))
        return " ".join(part for part in parts if part)

    def _rebuild_index(self) -> None:
        if not self._episodes:
            self._index = None
            return
        self._index = TfidfIndex(
            [self._episode_text(episode) for episode in self._episodes]
        )

    # ---- 对外 API ----

    @property
    def size(self) -> int:
        self._load()
        return len(self._episodes)

    async def record_episode(
        self,
        input_text: str,
        past_steps: list[tuple[str, str]] | None,
        response: str,
        session_id: str = "",
        skill_id: str = "",
        outcome: str = "",
        verification_passed: bool | None = None,
    ) -> str | None:
        """诊断结束后沉淀一条 episode；任何失败只记日志不影响主流程。

        新增（C2）：
        - `skill_id`：命中的 Skill；未命中留空字符串；
        - `outcome`：诊断结果标签（`success`/`partial`/`abandoned`/`failed` 等），
          由调用方按业务语义传入；空字符串表示未指定；
        - `verification_passed`：Skill 验收是否全部通过（未命中或未验收留 None）。
        """
        if not config.aiops_incident_memory_enabled:
            return None
        if not (input_text or "").strip() or not (response or "").strip():
            # 没有产出报告的失败诊断不进入记忆（避免污染相似检索）
            return None

        self._load()
        episode_id = f"ep-{int(time.time() * 1000)}-{random.randint(100, 999)}"
        episode = {
            "episode_id": episode_id,
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "input": _clamp(input_text, _MAX_INPUT_CHARS),
            "steps": [
                {
                    "description": _clamp(description, 200),
                    "result": _clamp(result, _MAX_STEP_RESULT_CHARS),
                }
                for description, result in (past_steps or [])
            ],
            "response_summary": _clamp(response, _MAX_RESPONSE_CHARS),
            # C2：Skill 归因与验收结果（老 episode 缺该字段时读侧按缺失处理）
            "skill_id": str(skill_id or "").strip(),
            "outcome": str(outcome or "").strip(),
            "verification_passed": verification_passed,
        }

        async with self._lock:
            try:
                self._episodes.append(episode)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(episode, ensure_ascii=False) + "\n")
                self._rotate_if_needed()
                self._rebuild_index()
                logger.info(f"事件记忆已沉淀 episode={episode_id}（共 {len(self._episodes)} 条）")
                return episode_id
            except Exception as exc:
                # 写入失败时回滚内存态，保持内存与磁盘一致
                if self._episodes and self._episodes[-1].get("episode_id") == episode_id:
                    self._episodes.pop()
                logger.warning(f"沉淀事件记忆失败（不影响诊断主流程）: {exc}")
                return None

    def recall(self, query: str, k: int | None = None) -> list[dict[str, Any]]:
        """检索相似历史事件，按相似度降序返回 [{'episode':..., 'score':...}]。"""
        if not config.aiops_incident_memory_enabled or not (query or "").strip():
            return []
        self._load()
        if not self._episodes or self._index is None:
            return []
        top_k = k or config.aiops_incident_memory_top_k
        hits = self._index.top_k(
            query, k=top_k, min_score=config.aiops_incident_memory_min_score
        )
        return [
            {"episode": self._episodes[index], "score": round(score, 4)}
            for index, score in hits
        ]

    def format_recalled_episodes(self, query: str, k: int | None = None) -> str:
        """把相似历史事件格式化为可注入 Planner prompt 的文本块。"""
        hits = self.recall(query, k)
        if not hits:
            return ""
        blocks: list[str] = []
        for rank, hit in enumerate(hits, start=1):
            episode = hit["episode"]
            created = str(episode.get("created_at", ""))[:19].replace("T", " ")
            lines = [
                f"### 历史事件 {rank}（{created}，相似度 {hit['score']}）",
                f"- 当时的任务: {episode.get('input', '')}",
            ]
            steps = episode.get("steps", [])
            if steps:
                summary = "；".join(
                    str(step.get("description", "")) for step in steps[:5]
                )
                lines.append(f"- 当时执行的步骤: {summary}")
            conclusion = str(episode.get("response_summary", ""))
            if conclusion:
                lines.append(f"- 当时的结论与处理: {conclusion}")
            blocks.append("\n".join(lines))
        return (
            "## 历史相似事件\n\n"
            "以下是历史诊断中与当前任务相似的事件及其结论，"
            "可参考当时的排查路径与根因，但必须用工具验证当前环境仍然成立：\n\n"
            + "\n\n".join(blocks)
        )


# 全局单例
incident_memory_service = IncidentMemoryService()
