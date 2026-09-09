"""证据接口包（零依赖）。

只定义证据数据结构与 Reader Protocol；不提供任何具体后端实现。
Codex / Trae 挂载场景下由宿主 Agent 自行采集证据，然后通过 CLI/MCP 的
``evidence_by_step`` 参数回传给渲染层；本包只承担"结构化契约"角色。

具体后端（如 curl prom_api / grep 日志）属于 ``[server]`` extra 的范畴，
放在 ``aiops_core/evidence/local_backends/`` 下（按需实现，非本 PR 目标）。
"""

from aiops_core.evidence.reader import (
    EvidenceItem,
    EvidenceReader,
    aggregate_evidence,
    normalize_evidence,
)

__all__ = [
    "EvidenceItem",
    "EvidenceReader",
    "aggregate_evidence",
    "normalize_evidence",
]
