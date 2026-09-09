"""Domain Profile 加载器（P0-3）。

profiles/<name>.yaml 结构（P0-3 只使用 planner/executor/replanner/tool_groups/
default_tool_prefixes/alert_ingest 六节；未识别键忽略以便未来向前兼容）：

```yaml
name: aiops
display_name: AIOps 智能 OnCall
planner:
  role_description: "专家级 SRE / AIOps 工程师"
  language: zh
  system_style: "先证据后结论，只读排查优先"
executor:
  role_description: "严格按步骤执行，不越权变更"
replanner:
  max_steps_before_force_respond: 5
alert_ingest:
  title_field: alertname
  severity_field: severity
  instance_field: instance
tool_groups:
  - keywords: ["日志", "log"]
    prefixes: ["search_log"]
default_tool_prefixes: ["retrieve_knowledge", "get_current_time"]
default_skills_dir: skills/aiops
```

设计取舍：
- 复用 skills/loader.py 的 `parse_simple_yaml`（轻量 YAML 子集），避免引入
  yaml 依赖；profiles 目录同样是运维视角写的 YAML，语法子集够用。
- Profile 是**进程级单例**（active_profile）；测试通过 `reset_profile_cache`
  重置。避免每次 Registry select 都做文件 IO。
- 未定义的字段 → tool_registry 层用「_FALLBACK_*」常量兜底，确保 profile
  文件删掉也不会影响服务；这层等价性由单测锁定。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from app.agent.skills.loader import parse_simple_yaml


@dataclass(frozen=True)
class ProfileToolGroup:
    """领域关键词 → 工具名/前缀组的一条映射。"""

    keywords: tuple[str, ...]
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class DomainProfile:
    """Domain Profile 只读快照。字段全部可选（未配置返回空/None）。"""

    name: str
    display_name: str = ""
    planner_role_description: str = ""
    planner_language: str = ""
    planner_system_style: str = ""
    executor_role_description: str = ""
    replanner_max_steps_before_force_respond: int | None = None
    alert_title_field: str = ""
    alert_severity_field: str = ""
    alert_instance_field: str = ""
    tool_groups: tuple[ProfileToolGroup, ...] = field(default_factory=tuple)
    default_tool_prefixes: tuple[str, ...] = field(default_factory=tuple)
    default_skills_dir: str = ""
    # C4：工具关键词/描述本地化模式：zh（默认）、en、bilingual
    tool_locale: str = ""
    # C6：按告警 severity 差异化的 incident 归并窗口秒数
    incident_dedup_windows_by_severity: tuple[tuple[str, float], ...] = field(
        default_factory=tuple
    )
    # C7：profile 追加的注入模式（作为正则字符串）
    injection_patterns: tuple[str, ...] = field(default_factory=tuple)
    source: str = ""

    def has_tool_groups(self) -> bool:
        """Profile 是否显式覆盖了工具关键词分组。未覆盖时 tool_registry 用兜底常量。"""
        return len(self.tool_groups) > 0

    def has_default_tool_prefixes(self) -> bool:
        return len(self.default_tool_prefixes) > 0

    def dedup_window_for_severity(self, severity: str, fallback: float) -> float:
        """按告警 severity 返回归并窗口秒数；未配置或匹配失败时使用 fallback。"""
        target = (severity or "").strip().lower()
        if not target:
            return fallback
        for key, value in self.incident_dedup_windows_by_severity:
            if key == target:
                return value
        return fallback


# ------------------------- 内部：解析工具 -------------------------


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item).strip() if item is not None else ""
        if text:
            result.append(text)
    return tuple(result)


def _as_int(value: Any) -> int | None:
    """把配置值转为 int；失败返回 None，让上层用兜底。"""
    if isinstance(value, bool):
        # bool 是 int 的子类，显式排除避免 True → 1 的误解
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_tool_groups(raw: Any) -> tuple[ProfileToolGroup, ...]:
    if not isinstance(raw, list):
        return ()
    groups: list[ProfileToolGroup] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        keywords = _as_str_tuple(entry.get("keywords"))
        prefixes = _as_str_tuple(entry.get("prefixes"))
        # 空组直接丢弃：关键词或前缀任一为空都无法生效
        if keywords and prefixes:
            groups.append(ProfileToolGroup(keywords=keywords, prefixes=prefixes))
    return tuple(groups)


def _profile_from_dict(name: str, raw: dict[str, Any], source: str) -> DomainProfile:
    """把 YAML dict 解析成 DomainProfile 快照。所有字段解析失败均使用默认值。"""
    planner_section = raw.get("planner") if isinstance(raw.get("planner"), dict) else {}
    executor_section = (
        raw.get("executor") if isinstance(raw.get("executor"), dict) else {}
    )
    replanner_section = (
        raw.get("replanner") if isinstance(raw.get("replanner"), dict) else {}
    )
    alert_section = (
        raw.get("alert_ingest") if isinstance(raw.get("alert_ingest"), dict) else {}
    )
    incident_section = (
        raw.get("incident") if isinstance(raw.get("incident"), dict) else {}
    )
    tools_section = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    safety_section = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}

    # C6：severity → window seconds
    dedup_windows: list[tuple[str, float]] = []
    raw_windows = incident_section.get("dedup_windows_by_severity")
    if isinstance(raw_windows, dict):
        for key, value in raw_windows.items():
            try:
                dedup_windows.append((str(key).strip().lower(), float(value)))
            except (TypeError, ValueError):
                continue

    # C7：injection patterns
    injection_patterns = _as_str_tuple(safety_section.get("injection_patterns"))

    return DomainProfile(
        name=name,
        display_name=str(raw.get("display_name") or "").strip(),
        planner_role_description=str(planner_section.get("role_description") or "").strip(),
        planner_language=str(planner_section.get("language") or "").strip(),
        planner_system_style=str(planner_section.get("system_style") or "").strip(),
        executor_role_description=str(
            executor_section.get("role_description") or ""
        ).strip(),
        replanner_max_steps_before_force_respond=_as_int(
            replanner_section.get("max_steps_before_force_respond")
        ),
        alert_title_field=str(alert_section.get("title_field") or "").strip(),
        alert_severity_field=str(alert_section.get("severity_field") or "").strip(),
        alert_instance_field=str(alert_section.get("instance_field") or "").strip(),
        tool_groups=_parse_tool_groups(raw.get("tool_groups")),
        default_tool_prefixes=_as_str_tuple(raw.get("default_tool_prefixes")),
        default_skills_dir=str(raw.get("default_skills_dir") or "").strip(),
        tool_locale=str(tools_section.get("locale") or "").strip(),
        incident_dedup_windows_by_severity=tuple(dedup_windows),
        injection_patterns=injection_patterns,
        source=source,
    )


# ------------------------- 路径解析 -------------------------


_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILTIN_PROFILES_DIR = _REPO_ROOT / "profiles"


def _candidate_paths(name: str) -> list[Path]:
    """按优先级返回可能的 profile 文件路径。返回顺序即查找顺序。"""
    return [
        _BUILTIN_PROFILES_DIR / f"{name}.yaml",
        _BUILTIN_PROFILES_DIR / f"{name}.yml",
    ]


def load_profile(name: str) -> DomainProfile:
    """按名字加载 profile；找不到或解析失败时返回同名空 profile（不抛异常）。

    调用方使用 `profile.has_tool_groups()` 判定是否应用 profile 值。
    空 profile 语义 = 使用 tool_registry 里的兜底常量。
    """
    normalized = (name or "").strip() or "aiops"
    for path in _candidate_paths(normalized):
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"Profile 文件读取失败 {path}: {exc}")
            continue
        try:
            parsed = parse_simple_yaml(text)
        except Exception as exc:  # noqa: BLE001 - 解析器可能抛任意异常
            logger.warning(f"Profile YAML 解析失败 {path}: {exc}")
            continue
        if not isinstance(parsed, dict):
            logger.warning(f"Profile 根节点不是 dict，忽略 {path}")
            continue
        # 允许 profile 内 name 字段覆盖文件名；缺失时用传入 name
        profile_name = str(parsed.get("name") or normalized).strip() or normalized
        return _profile_from_dict(profile_name, parsed, source=f"file:{path.name}")

    logger.info(
        f"未找到 profile '{normalized}'（查找目录 {_BUILTIN_PROFILES_DIR}），"
        "回退空 profile → tool_registry 使用内置常量"
    )
    return DomainProfile(name=normalized, source="fallback:empty")


# ------------------------- 单例缓存 -------------------------


_cache_lock = threading.RLock()
_cached_profile: DomainProfile | None = None
_cached_profile_name: str | None = None


def _resolve_active_profile_name() -> str:
    """从 config 读取当前 profile 名字。config 导入失败时回落 'aiops'。"""
    try:
        from app.config import config

        name = getattr(config, "aiops_domain_profile", "") or "aiops"
    except Exception as exc:  # noqa: BLE001 - 启动阶段任何异常都不能拖垮 profile 层
        logger.warning(f"读取 aiops_domain_profile 配置失败，使用 'aiops': {exc}")
        name = "aiops"
    return str(name).strip() or "aiops"


def get_active_profile() -> DomainProfile:
    """返回当前进程的 profile 快照；文件级缓存。"""
    global _cached_profile, _cached_profile_name
    name = _resolve_active_profile_name()
    with _cache_lock:
        if _cached_profile is None or _cached_profile_name != name:
            _cached_profile = load_profile(name)
            _cached_profile_name = name
        return _cached_profile


def reset_profile_cache() -> None:
    """清空进程级 profile 缓存（供测试与 hot reload 使用）。"""
    global _cached_profile, _cached_profile_name
    with _cache_lock:
        _cached_profile = None
        _cached_profile_name = None
