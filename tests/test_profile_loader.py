"""P0-3：Domain Profile 加载器 + 与硬编码常量的等价性回归。"""

from types import SimpleNamespace

import pytest

from app.agent.profiles import (
    ProfileToolGroup,
    get_active_profile,
    load_profile,
    reset_profile_cache,
)
from app.agent.profiles.loader import _profile_from_dict
from app.agent.tool_registry import (
    DEFAULT_TOOL_PREFIXES,
    TOOL_GROUPS,
    _active_default_prefixes,
    _active_tool_groups,
    tool_registry,
)


@pytest.fixture(autouse=True)
def _reset_profile_cache_between_tests():
    reset_profile_cache()
    yield
    reset_profile_cache()


def test_load_profile_aiops_yaml_parses_all_sections():
    """内置 aiops.yaml 能被完整解析成 DomainProfile 快照。"""
    profile = load_profile("aiops")

    assert profile.name == "aiops"
    assert profile.display_name == "AIOps 智能 OnCall"
    assert profile.planner_role_description == "专家级 SRE / AIOps 工程师"
    assert profile.planner_language == "zh"
    assert profile.executor_role_description == "严格按步骤执行，不越权变更"
    assert profile.replanner_max_steps_before_force_respond == 5
    assert profile.alert_title_field == "alertname"
    assert profile.alert_severity_field == "severity"
    assert profile.alert_instance_field == "instance"
    assert profile.default_skills_dir == "skills/aiops"
    assert profile.source.startswith("file:")


def test_load_profile_missing_returns_empty_profile():
    """未找到 profile 文件 → 返回空 profile，不抛异常，has_* 全部为 False。"""
    profile = load_profile("__no_such_profile__")
    assert profile.name == "__no_such_profile__"
    assert profile.source == "fallback:empty"
    assert profile.has_tool_groups() is False
    assert profile.has_default_tool_prefixes() is False


def test_load_profile_empty_name_falls_back_to_aiops():
    """空字符串名字 → 走默认 aiops profile（读到内置 YAML）。"""
    profile = load_profile("")
    assert profile.name == "aiops"
    assert profile.has_tool_groups() is True


def test_active_tool_groups_are_equivalent_to_hardcoded_constants():
    """P0-3 核心验收：AIOPS_DOMAIN_PROFILE=aiops 时，profile 加载出的
    tool_groups 与旧硬编码常量在结构上完全等价（顺序 + 内容）。"""
    active = _active_tool_groups()

    # 两组结构相同、顺序相同、每对关键词/前缀集合相同
    assert len(active) == len(TOOL_GROUPS)
    for (active_keywords, active_prefixes), (
        legacy_keywords,
        legacy_prefixes,
    ) in zip(active, TOOL_GROUPS, strict=True):
        assert set(active_keywords) == set(legacy_keywords)
        assert set(active_prefixes) == set(legacy_prefixes)


def test_active_default_prefixes_equivalent_to_hardcoded_constants():
    """default_tool_prefixes 也必须与硬编码常量集合等价。"""
    active = _active_default_prefixes()
    assert set(active) == set(DEFAULT_TOOL_PREFIXES)


def test_tool_registry_select_result_unchanged_when_profile_active():
    """profile 生效后，Registry.select 结果不能相对旧行为退步。"""
    # 用与 test_aiops_tool_routing.py 相同的输入
    tools = [
        SimpleNamespace(name=name, description=f"{name} 的描述")
        for name in (
            "retrieve_knowledge",
            "get_current_time",
            "mysql_query",
            "mysql_explain",
            "redis_info",
            "query_cpu_metrics",
        )
    ]
    selected = [t.name for t in tool_registry.select(
        "数据库出现慢查询，请分析", tools, limit=12
    )]
    assert "mysql_query" in selected
    assert "mysql_explain" in selected
    assert "redis_info" not in selected


def test_tool_registry_uses_profile_override(monkeypatch):
    """profile 显式覆盖 tool_groups 时，Registry 应按 profile 的关键词分组路由。"""
    from app.agent.profiles.loader import DomainProfile

    override = DomainProfile(
        name="test-profile",
        tool_groups=(
            ProfileToolGroup(keywords=("特殊关键词",), prefixes=("mysql_",)),
        ),
        default_tool_prefixes=("get_current_time",),
        source="test:inline",
    )

    # tool_registry 里的 helpers 通过 `from app.agent.profiles import
    # get_active_profile` 拿到当前 profile，patch 包级 attr 即可覆盖
    import app.agent.profiles as profiles_pkg

    monkeypatch.setattr(profiles_pkg, "get_active_profile", lambda: override)

    tools = [
        SimpleNamespace(name=name, description=f"{name} 的描述")
        for name in ("mysql_query", "redis_info", "get_current_time")
    ]

    # 「特殊关键词」触发 profile 的分组，选中 mysql_ 与 default 里的 get_current_time
    selected = [t.name for t in tool_registry.select("特殊关键词", tools, limit=12)]
    assert "mysql_query" in selected
    assert "get_current_time" in selected
    # 未在 profile 分组也不在 default 里 → 不应被选中
    assert "redis_info" not in selected


def test_get_active_profile_uses_config_and_caches(monkeypatch):
    """get_active_profile 读 config.aiops_domain_profile，且缓存到 reset 为止。"""
    call_count = {"n": 0}

    real_load = load_profile

    def counted_load(name):
        call_count["n"] += 1
        return real_load(name)

    from app.agent.profiles import loader as profile_loader

    monkeypatch.setattr(profile_loader, "load_profile", counted_load)

    profile1 = get_active_profile()
    profile2 = get_active_profile()
    assert profile1 is profile2  # 命中缓存
    assert call_count["n"] == 1

    reset_profile_cache()
    profile3 = get_active_profile()
    assert call_count["n"] == 2
    # profile 语义不变（同一份 yaml）
    assert profile3.name == profile1.name


def test_profile_from_dict_handles_partial_and_invalid_fields():
    """部分字段缺失或类型错误 → 使用默认值，不抛异常。"""
    profile = _profile_from_dict(
        name="partial",
        raw={
            "planner": {"role_description": " some role "},
            # replanner.max_steps_before_force_respond 是字符串 → 应能转 int
            "replanner": {"max_steps_before_force_respond": "7"},
            # tool_groups 元素类型不对，应被丢弃
            "tool_groups": [
                "not a dict",
                {"keywords": [], "prefixes": ["x"]},   # 空关键词 → 丢弃
                {"keywords": ["k"], "prefixes": []},   # 空前缀 → 丢弃
                {"keywords": ["k1"], "prefixes": ["p1"]},
            ],
            # default_tool_prefixes 非 list → 视为空
            "default_tool_prefixes": "not-a-list",
        },
        source="test:partial",
    )

    assert profile.planner_role_description == "some role"
    assert profile.replanner_max_steps_before_force_respond == 7
    assert profile.tool_groups == (
        ProfileToolGroup(keywords=("k1",), prefixes=("p1",)),
    )
    assert profile.default_tool_prefixes == ()
    assert profile.has_default_tool_prefixes() is False
    assert profile.has_tool_groups() is True


# ------------------- P1-2 C4/C6/C7：新增配置字段解析 -------------------


def test_c4_tool_locale_parsed_from_yaml():
    """C4：aiops.yaml 中的 tools.locale 能正确解析为 profile.tool_locale。"""
    profile = load_profile("aiops")
    assert profile.tool_locale == "zh"


def test_c4_tool_locale_missing_returns_empty_string():
    """C4：未配置 tools.locale 时保持空串，工具层视为默认中文。"""
    profile = _profile_from_dict(name="test", raw={}, source="test:inline")
    assert profile.tool_locale == ""


def test_c6_dedup_windows_by_severity_parsed_correctly():
    """C6：aiops.yaml 中的 incident.dedup_windows_by_severity 能被解析成 tuple 结构。"""
    profile = load_profile("aiops")
    mapping = dict(profile.incident_dedup_windows_by_severity)
    assert mapping["critical"] == 60.0
    assert mapping["error"] == 120.0
    assert mapping["warning"] == 300.0
    assert mapping["info"] == 600.0


def test_c6_dedup_window_lookup_returns_configured_and_fallback():
    """C6：dedup_window_for_severity 命中返回配置值，未命中/空值返回 fallback。"""
    profile = load_profile("aiops")
    assert profile.dedup_window_for_severity("CRITICAL", fallback=999.0) == 60.0
    assert profile.dedup_window_for_severity("unknown-sev", fallback=888.0) == 888.0
    assert profile.dedup_window_for_severity("", fallback=777.0) == 777.0


def test_c6_dedup_windows_ignores_invalid_values():
    """C6：非数值型 severity 窗口应被忽略而非抛异常。"""
    profile = _profile_from_dict(
        name="test",
        raw={
            "incident": {
                "dedup_windows_by_severity": {
                    "critical": "not-a-number",
                    "error": 42,
                }
            }
        },
        source="test:inline",
    )
    mapping = dict(profile.incident_dedup_windows_by_severity)
    assert "critical" not in mapping
    assert mapping["error"] == 42.0


def test_c7_injection_patterns_parsed_from_yaml():
    """C7：aiops.yaml 中的 safety.injection_patterns 能被解析（当前 aiops 为空）。"""
    profile = load_profile("aiops")
    assert profile.injection_patterns == ()


def test_c7_injection_patterns_parsed_from_dict():
    """C7：显式配置注入模式时按序解析为字符串 tuple。"""
    profile = _profile_from_dict(
        name="secops",
        raw={
            "safety": {
                "injection_patterns": [
                    r"(?i)curl\s+.*\|\s*sh",
                    r"(?i)rm\s+-rf\s+/",
                ]
            }
        },
        source="test:inline",
    )
    assert len(profile.injection_patterns) == 2
    assert r"(?i)curl\s+.*\|\s*sh" in profile.injection_patterns
