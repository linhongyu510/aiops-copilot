"""证据数据结构 + Reader 契约（零依赖）。

设计原则：
- 只定义 Protocol 与 dataclass；不引入 httpx / prometheus / opentelemetry 等具体依赖；
- ``EvidenceItem`` 是 Executor / CLI / MCP 三者共享的**唯一**证据交换格式；
- ``normalize_evidence`` 把宿主 Agent 回传的自由 dict / str 归一化成 EvidenceItem，
  这样后续 report renderer 只需要处理一种结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class EvidenceItem:
    """单条证据：一次工具调用的 (输入摘要, 输出摘要, 状态)。"""

    step_id: str
    tool: str = ""
    summary: str = ""
    raw: Any = None
    status: str = "ok"
    error: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "summary": self.summary,
            "raw": self.raw,
            "status": self.status,
            "error": self.error,
            "extras": dict(self.extras),
        }


@runtime_checkable
class EvidenceReader(Protocol):
    """证据采集器契约：给定一个 PlanStep dict，返回一条 EvidenceItem。

    宿主可以按需实现（如 CLI 侧从 --evidence file 读回放、MCP 侧调用外部工具），
    aiops_core 自身不绑定任何实现；因此本 Protocol 也不做 ABC 检查。
    """

    def read(self, step: dict[str, Any]) -> EvidenceItem: ...


def normalize_evidence(step_id: str, raw: Any) -> EvidenceItem:
    """把宿主 Agent 回传的证据（str / dict / EvidenceItem）统一成 EvidenceItem。

    - str → summary=str，status=ok；
    - dict → 按已知键抽取，未知键塞进 extras；
    - EvidenceItem → 原样返回；
    - None → 空占位，status=missing。
    """
    if raw is None:
        return EvidenceItem(step_id=step_id, status="missing", summary="(无证据)")
    if isinstance(raw, EvidenceItem):
        if not raw.step_id:
            return EvidenceItem(
                step_id=step_id,
                tool=raw.tool,
                summary=raw.summary,
                raw=raw.raw,
                status=raw.status,
                error=raw.error,
                extras=dict(raw.extras),
            )
        return raw
    if isinstance(raw, str):
        return EvidenceItem(step_id=step_id, summary=raw.strip())
    if isinstance(raw, dict):
        known = {"tool", "summary", "raw", "status", "error", "extras"}
        extras = {k: v for k, v in raw.items() if k not in known and k != "step_id"}
        return EvidenceItem(
            step_id=step_id,
            tool=str(raw.get("tool", "")),
            summary=str(raw.get("summary") or ""),
            raw=raw.get("raw"),
            status=str(raw.get("status", "ok")),
            error=str(raw.get("error", "")),
            extras=extras,
        )
    return EvidenceItem(step_id=step_id, summary=str(raw))


def aggregate_evidence(evidence_by_step: dict[str, Any]) -> list[EvidenceItem]:
    """把 ``{step_id: raw}`` 归一化为按插入顺序的 EvidenceItem 列表。"""
    if not isinstance(evidence_by_step, dict):
        return []
    return [normalize_evidence(step_id, raw) for step_id, raw in evidence_by_step.items()]
