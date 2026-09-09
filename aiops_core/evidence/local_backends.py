"""``--evidence local`` 模式下的零依赖证据采集后端。

设计原则：
- **只用标准库**：``urllib`` / ``subprocess`` / ``pathlib``；不引入 ``httpx`` 或
  ``requests``。这样 ``pip install aiops-copilot`` 默认档次即可跑 ``--evidence local``；
- **只暴露有限、可预期的采集能力**：http_probe / read_file / shell（限白名单命令）；
  更复杂的采集（Prom 查询 / 日志检索 / 拓扑）应交给宿主 Agent 或 ``[server]`` extra；
- **失败如实报告**：任何异常都翻译成 ``EvidenceItem(status="error", error=...)``，
  报告渲染器负责如实呈现，绝不 fake。

Step 到后端的映射通过 ``PlanStep.tool_hint``（首选）或 ``tool_args_template.type``
（后备）来路由。未知类型走 ``passthrough`` 后端，把 step 描述本身作为占位摘要，
让宿主 Agent 后续接管。
"""

from __future__ import annotations

import json
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from aiops_core.evidence.reader import EvidenceItem

# --------------------------- 白名单 shell 命令 ---------------------------

# 只放行只读、可预期的诊断命令。任何未在此集合中的可执行都会被拒绝，
# 避免 ``--evidence local`` 变成一把可被 Skill 作者滥用的 shell。
_SHELL_ALLOWLIST: set[str] = {
    "cat",
    "head",
    "tail",
    "wc",
    "ls",
    "date",
    "uname",
    "pwd",
    "echo",
    "grep",
    "awk",
    "sed",
    "sort",
    "uniq",
    "cut",
    "tr",
    "df",
    "du",
    "ps",
    "top",
    "kubectl",
    "curl",
}

_HTTP_TIMEOUT_SEC = 5.0
_SHELL_TIMEOUT_SEC = 10.0
_MAX_SUMMARY_CHARS = 2000


def _clip(text: str) -> str:
    if len(text) <= _MAX_SUMMARY_CHARS:
        return text
    return text[:_MAX_SUMMARY_CHARS] + "……[已截断]"


# --------------------------- 各类后端 ---------------------------


def _backend_http_probe(step_id: str, args: dict[str, Any]) -> EvidenceItem:
    url = str(args.get("url") or "").strip()
    if not url:
        return EvidenceItem(
            step_id=step_id,
            tool="http_probe",
            status="error",
            error="缺少 args.url",
        )
    method = str(args.get("method") or "GET").upper()
    headers = args.get("headers") or {}
    timeout = float(args.get("timeout") or _HTTP_TIMEOUT_SEC)
    data = args.get("body")
    if isinstance(data, (dict, list)):
        body_bytes: bytes | None = json.dumps(data).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    elif isinstance(data, str):
        body_bytes = data.encode("utf-8")
    else:
        body_bytes = None

    req = urllib.request.Request(url, data=body_bytes, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return EvidenceItem(
            step_id=step_id,
            tool="http_probe",
            status="error",
            error=f"HTTP {e.code} {e.reason}",
            summary=_clip((e.read() or b"").decode("utf-8", errors="replace")),
            extras={"url": url, "method": method},
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return EvidenceItem(
            step_id=step_id,
            tool="http_probe",
            status="error",
            error=str(e),
            extras={"url": url, "method": method},
        )
    return EvidenceItem(
        step_id=step_id,
        tool="http_probe",
        status="ok" if 200 <= status < 400 else "warn",
        summary=_clip(body),
        extras={"url": url, "method": method, "status_code": status},
    )


def _backend_read_file(step_id: str, args: dict[str, Any]) -> EvidenceItem:
    path_str = str(args.get("path") or "").strip()
    if not path_str:
        return EvidenceItem(
            step_id=step_id,
            tool="read_file",
            status="error",
            error="缺少 args.path",
        )
    path = Path(path_str).expanduser()
    if not path.is_file():
        return EvidenceItem(
            step_id=step_id,
            tool="read_file",
            status="missing",
            error=f"文件不存在：{path}",
            extras={"path": str(path)},
        )
    max_bytes = int(args.get("max_bytes") or 65536)
    try:
        raw = path.read_bytes()[:max_bytes]
    except OSError as e:
        return EvidenceItem(
            step_id=step_id,
            tool="read_file",
            status="error",
            error=str(e),
            extras={"path": str(path)},
        )
    return EvidenceItem(
        step_id=step_id,
        tool="read_file",
        status="ok",
        summary=_clip(raw.decode("utf-8", errors="replace")),
        extras={"path": str(path), "bytes": len(raw)},
    )


def _backend_shell(step_id: str, args: dict[str, Any]) -> EvidenceItem:
    """严格白名单 shell：只放行只读诊断命令。

    入参形式二选一：
    - ``args.command``: 字符串，用 ``shlex.split`` 拆分；
    - ``args.argv``: 字符串数组，直接作为 argv。
    """
    argv = args.get("argv")
    if not argv:
        command = str(args.get("command") or "").strip()
        if not command:
            return EvidenceItem(
                step_id=step_id,
                tool="shell",
                status="error",
                error="缺少 args.command 或 args.argv",
            )
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return EvidenceItem(
                step_id=step_id,
                tool="shell",
                status="error",
                error=f"shell 命令解析失败：{e}",
            )
    argv = [str(x) for x in argv]
    if not argv:
        return EvidenceItem(step_id=step_id, tool="shell", status="error", error="argv 为空")
    bin_name = Path(argv[0]).name
    if bin_name not in _SHELL_ALLOWLIST:
        return EvidenceItem(
            step_id=step_id,
            tool="shell",
            status="denied",
            error=f"命令 `{bin_name}` 不在白名单；如需扩展请显式修改 _SHELL_ALLOWLIST",
            extras={"argv": argv},
        )
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT_SEC,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return EvidenceItem(
            step_id=step_id,
            tool="shell",
            status="error",
            error=str(e),
            extras={"argv": argv},
        )
    combined = proc.stdout or ""
    if proc.stderr:
        combined = combined + ("\n---stderr---\n" + proc.stderr if combined else proc.stderr)
    return EvidenceItem(
        step_id=step_id,
        tool="shell",
        status="ok" if proc.returncode == 0 else "warn",
        summary=_clip(combined),
        error="" if proc.returncode == 0 else f"exit={proc.returncode}",
        extras={"argv": argv, "exit_code": proc.returncode},
    )


def _backend_passthrough(step_id: str, step: dict[str, Any]) -> EvidenceItem:
    """未知 tool_hint 时的兜底：把 step 描述本身作为占位摘要，status=skipped。"""
    return EvidenceItem(
        step_id=step_id,
        tool=str(step.get("tool_hint") or "unknown"),
        status="skipped",
        summary=str(step.get("description") or "(空描述)"),
        extras={
            "reason": "no_local_backend_for_this_tool_hint",
            "template": step.get("tool_args_rendered") or step.get("tool_args_template"),
        },
    )


# --------------------------- 路由 ---------------------------


def _resolve_args(step: dict[str, Any]) -> dict[str, Any]:
    """优先取 tool_args_rendered（含 context 替换），退化为原始 template。"""
    args: Any = step.get("tool_args_rendered") or step.get("tool_args_template")
    if isinstance(args, dict):
        return args
    return {}


def collect_local_evidence(step: dict[str, Any]) -> EvidenceItem:
    """把一个 PlanStep 路由到对应本地后端，返回一条 EvidenceItem。"""
    step_id = str(step.get("step_id") or "")
    tool_hint = str(step.get("tool_hint") or "").lower()
    args = _resolve_args(step)

    # 允许通过 args.type 显式覆盖 tool_hint（宿主编排的 escape hatch）
    tool_type = str(args.get("type") or tool_hint)

    if tool_type in ("http_probe", "http", "https", "curl"):
        return _backend_http_probe(step_id, args)
    if tool_type in ("read_file", "file", "cat_file"):
        return _backend_read_file(step_id, args)
    if tool_type in ("shell", "bash", "cmd"):
        return _backend_shell(step_id, args)
    return _backend_passthrough(step_id, step)


__all__ = [
    "collect_local_evidence",
]
