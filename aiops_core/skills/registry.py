"""Skill Registry：加载、匹配、校验、度量（零依赖版本）。

从 ``app.agent.skills.registry`` 抽出，去除对 ``app.config`` / ``app.services.playbook_service`` /
``app.agent.tool_registry`` 的硬依赖，通过可选注入点承接：

- ``tools_catalog_provider``：可注入的工具目录探针（``() -> set[str]``），用于 required_tools 校验。
  ``None`` 时校验跳过（内核默认不感知具体工具目录）。
- ``legacy_playbooks_provider``：可注入的 legacy playbook 提供器（``() -> list[dict]``）。
  ``None`` 时不加载 legacy 兜底（Skill YAML 目录为唯一来源）。

匹配得分融合三路信号：
- ``alert_names`` 精确命中：+0.6
- ``label_selectors`` 全部正则命中：+0.2
- ``symptoms`` TF-IDF 余弦：原始分×0.6

分数上限归一化到 1.0；低于 ``SkillTrigger.min_score`` 的候选丢弃。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from aiops_core.skills.loader import load_skills_dir, playbook_to_skill
from aiops_core.skills.models import SkillPack
from aiops_core.skills.text_similarity import TfidfIndex


@dataclass
class SkillMatch:
    """匹配结果：Skill 引用 + 融合得分 + 命中理由。"""

    skill: SkillPack
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class SkillMetric:
    activated: int = 0
    succeeded: int = 0
    abandoned: int = 0
    disabled_reason: str | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class SkillRegistry:
    """Skill 加载/匹配/度量的单例。构造惰性，reload() 显式刷新。"""

    def __init__(
        self,
        skills_dir: str | Path | None = None,
        include_legacy_playbooks: bool = True,
        *,
        tools_catalog_provider: Callable[[], set[str]] | None = None,
        legacy_playbooks_provider: Callable[[], list[dict[str, Any]]] | None = None,
        default_top_k: int | None = None,
    ) -> None:
        self._skills_dir = Path(skills_dir) if skills_dir else None
        self._include_legacy = include_legacy_playbooks
        self._tools_catalog_provider = tools_catalog_provider
        self._legacy_playbooks_provider = legacy_playbooks_provider
        self._default_top_k = (
            default_top_k
            if default_top_k is not None
            else _env_int("AIOPS_SKILL_TOP_K", 3)
        )
        self._skills: list[SkillPack] = []
        self._index: TfidfIndex | None = None
        self._metrics: dict[str, SkillMetric] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self.source_summary: dict[str, int] = {}

    # ------------------------- Loading -------------------------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_internal()
            self._loaded = True

    def _load_internal(self) -> None:
        packs: list[SkillPack] = []
        source_counts: dict[str, int] = {}

        if self._skills_dir:
            for pack in load_skills_dir(self._skills_dir):
                packs.append(pack)
                source_counts[pack.source or "file"] = (
                    source_counts.get(pack.source or "file", 0) + 1
                )

        if self._include_legacy and self._legacy_playbooks_provider is not None:
            try:
                legacy_raw = self._legacy_playbooks_provider() or []
            except Exception as exc:
                logger.warning(f"legacy playbook 加载器抛异常，忽略：{exc}")
                legacy_raw = []
            for playbook in legacy_raw:
                pack = playbook_to_skill(playbook, source="builtin-demo")
                if pack is None:
                    continue
                if pack.skill_id in {existing.skill_id for existing in packs}:
                    continue
                packs.append(pack)
                source_counts[pack.source or "legacy"] = (
                    source_counts.get(pack.source or "legacy", 0) + 1
                )

        self._validate_against_tools(packs)
        self._skills = packs
        self._rebuild_index()
        self._metrics = {
            pack.skill_id: SkillMetric(disabled_reason=pack.disabled_reason) for pack in packs
        }
        self.source_summary = source_counts
        logger.info(
            f"Skill Registry 加载完成：{len(packs)} 个 Skill，来源分布={source_counts}"
        )

    def _validate_against_tools(self, packs: list[SkillPack]) -> None:
        """检查 required_tools 是否在注入的 tools_catalog 内；缺失则标 disabled_reason。

        未注入 ``tools_catalog_provider`` 时静默跳过 —— 内核不感知具体工具目录。
        """
        if self._tools_catalog_provider is None:
            return
        try:
            catalog_names = set(self._tools_catalog_provider())
        except Exception as exc:
            logger.warning(f"tools_catalog_provider 不可用，Skill required_tools 校验跳过: {exc}")
            return
        for pack in packs:
            missing = [name for name in pack.required_tools if name not in catalog_names]
            if missing:
                pack.disabled_reason = f"required_tools_missing:{','.join(missing)}"

    def _rebuild_index(self) -> None:
        if not self._skills:
            self._index = None
            return
        docs = [self._skill_text(pack) for pack in self._skills]
        self._index = TfidfIndex(docs)

    @staticmethod
    def _skill_text(pack: SkillPack) -> str:
        parts: list[str] = [pack.title, pack.description]
        parts.extend(pack.trigger.symptoms)
        parts.extend(pack.trigger.alert_names)
        parts.extend(pack.tags)
        return " ".join(str(item) for item in parts if item)

    def reload(self) -> None:
        with self._lock:
            self._loaded = False
            self._load_internal()
            self._loaded = True

    # ------------------------- Query -------------------------

    def list_skills(self) -> list[SkillPack]:
        self._ensure_loaded()
        return list(self._skills)

    def get(self, skill_id: str) -> SkillPack | None:
        self._ensure_loaded()
        for pack in self._skills:
            if pack.skill_id == skill_id:
                return pack
        return None

    def catalog_snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "skill_id": pack.skill_id,
                "title": pack.title,
                "domain": pack.domain,
                "version": pack.version,
                "description": pack.description,
                "review_status": pack.review_status,
                "required_role": pack.required_role,
                "required_tools": list(pack.required_tools),
                "step_count": len(pack.steps),
                "has_verifications": bool(pack.verifications),
                "tags": list(pack.tags),
                "source": pack.source,
                "disabled_reason": pack.disabled_reason,
            }
            for pack in self.list_skills()
        ]

    # ------------------------- Match -------------------------

    def match(
        self,
        query: str,
        alert_labels: dict[str, str] | None = None,
        k: int | None = None,
    ) -> list[SkillMatch]:
        """按融合信号返回 top-k SkillMatch，按分数降序。"""
        self._ensure_loaded()
        if not self._skills:
            return []
        top_k = k or max(self._default_top_k, 2)
        labels = alert_labels or {}

        semantic_scores: list[float]
        if self._index is not None and (query or "").strip():
            semantic_scores = self._index.score(query)
        else:
            semantic_scores = [0.0] * len(self._skills)

        results: list[SkillMatch] = []
        for index, pack in enumerate(self._skills):
            if not pack.is_enabled():
                continue
            reasons: list[str] = []
            score = 0.0

            alert_name = labels.get("alertname") or ""
            if alert_name and pack.trigger.alert_names:
                for alert in pack.trigger.alert_names:
                    if alert == alert_name:
                        score += 0.6
                        reasons.append(f"alertname={alert}")
                        break

            if pack.trigger.label_selectors:
                all_match = True
                matched_pairs: list[str] = []
                for key, pattern in pack.trigger.label_selectors.items():
                    value = labels.get(key)
                    if value is None:
                        all_match = False
                        break
                    try:
                        if not re.search(pattern, value):
                            all_match = False
                            break
                    except re.error:
                        all_match = False
                        break
                    matched_pairs.append(f"{key}={value}")
                if all_match and matched_pairs:
                    score += 0.2
                    reasons.append("labels:" + ",".join(matched_pairs))

            semantic = semantic_scores[index]
            if semantic >= pack.trigger.min_score:
                weighted = semantic * 0.6
                score += weighted
                reasons.append(f"symptoms≈{semantic:.2f}")

            if score <= 0.0 or not reasons:
                continue

            score = min(score, 1.0)
            results.append(SkillMatch(skill=pack, score=round(score, 4), reasons=reasons))

        results.sort(key=lambda m: -m.score)
        return results[:top_k]

    def format_matched_skills(
        self,
        query: str,
        alert_labels: dict[str, str] | None = None,
        k: int | None = None,
    ) -> str:
        matched = self.match(query, alert_labels=alert_labels, k=k)
        if not matched:
            return ""
        blocks: list[str] = []
        for item in matched:
            pack = item.skill
            lines = [
                f"### Skill: {pack.title}（id={pack.skill_id}, 匹配度 {item.score}）",
                f"- 命中信号: {'；'.join(item.reasons)}",
            ]
            if pack.description:
                lines.append(f"- 说明: {pack.description}")
            step_lines = "\n".join(
                f"  {i}. [{step.id}] {step.description}"
                + (f"（建议工具: {step.tool_hint}）" if step.tool_hint else "")
                for i, step in enumerate(pack.steps, 1)
            )
            lines.append(f"- 标准步骤:\n{step_lines}")
            if pack.verifications:
                verify_lines = "；".join(
                    f"{v.tool} => {v.success_expr}" for v in pack.verifications
                )
                lines.append(f"- 验证清单: {verify_lines}")
            if pack.tags:
                lines.append(f"- 标签: {', '.join(pack.tags)}")
            blocks.append("\n".join(lines))
        return (
            "## 匹配 Skill\n\n"
            "以下是 Skill Registry 中与当前任务匹配的可执行预案。"
            "命中 Skill 时 Plan 会直接采用 skill.steps；"
            "如仍有前置未验证的条件，必须先用工具校验再执行修复动作：\n\n"
            + "\n\n".join(blocks)
        )

    # ------------------------- Metrics -------------------------

    def record_activation(self, skill_id: str) -> None:
        with self._lock:
            metric = self._metrics.setdefault(skill_id, SkillMetric())
            metric.activated += 1

    def record_outcome(self, skill_id: str, outcome: str) -> None:
        with self._lock:
            metric = self._metrics.setdefault(skill_id, SkillMetric())
            if outcome == "success":
                metric.succeeded += 1
            elif outcome == "abandoned":
                metric.abandoned += 1

    def metrics_snapshot(self) -> list[dict[str, Any]]:
        self._ensure_loaded()
        return [
            {
                "skill_id": skill_id,
                "activated": metric.activated,
                "succeeded": metric.succeeded,
                "abandoned": metric.abandoned,
                "success_rate": (
                    round(metric.succeeded / metric.activated, 4)
                    if metric.activated
                    else None
                ),
                "disabled_reason": metric.disabled_reason,
            }
            for skill_id, metric in self._metrics.items()
        ]


# 全局单例（tests 通过模块变量重置）
skill_registry: SkillRegistry | None = None

# 允许 app 层注入 legacy playbook / tool catalog 探针；只在首次实例化前生效。
_pending_legacy_provider: Callable[[], list[dict[str, Any]]] | None = None
_pending_tools_provider: Callable[[], set[str]] | None = None


def _default_skills_dir() -> str | None:
    """默认 skills 目录 = AIOPS_SKILLS_DIR 或 <project_root>/skills/aiops。"""
    candidate = os.getenv("AIOPS_SKILLS_DIR", "").strip()
    if candidate:
        return candidate
    # aiops_core/skills/registry.py → parents[2] 即项目根
    default_dir = Path(__file__).resolve().parents[2] / "skills" / "aiops"
    if default_dir.exists():
        return str(default_dir)
    return None


def register_legacy_playbook_provider(
    provider: Callable[[], list[dict[str, Any]]] | None,
) -> None:
    """让 app/ 层在启动时注入 BUILTIN_DEMO_PLAYBOOKS 兜底。仅在 registry 未装配前生效。"""
    global _pending_legacy_provider
    _pending_legacy_provider = provider


def register_tools_catalog_provider(
    provider: Callable[[], set[str]] | None,
) -> None:
    """让 app/ 层注入 tool_registry.catalog 名单以启用 required_tools 校验。"""
    global _pending_tools_provider
    _pending_tools_provider = provider


def get_skill_registry() -> SkillRegistry:
    """获取 Skill Registry 单例；首次访问按环境变量装配 skills_dir。"""
    global skill_registry
    if skill_registry is None:
        skill_registry = SkillRegistry(
            skills_dir=_default_skills_dir(),
            tools_catalog_provider=_pending_tools_provider,
            legacy_playbooks_provider=_pending_legacy_provider,
        )
    return skill_registry
