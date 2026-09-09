"""P0-1：Skill Registry 加载 / 匹配 / 度量 / 校验闭环。

不依赖任何外部服务：所有工具校验、YAML 解析都在本进程内完成。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.skills.loader import parse_simple_yaml, playbook_to_skill
from app.agent.skills.models import SkillPack, SkillStep, SkillTrigger, SkillVerification
from app.agent.skills.registry import SkillRegistry


def _demo_skill(skill_id: str, symptoms: list[str], alert: str = "", tool: str = "prom_query") -> SkillPack:
    return SkillPack(
        skill_id=skill_id,
        title=f"demo-{skill_id}",
        trigger=SkillTrigger(symptoms=symptoms, alert_names=[alert] if alert else []),
        required_tools=[tool],
        steps=[SkillStep(id="check", description="查询指标", tool_hint=tool)],
        verifications=[SkillVerification(tool=tool, args={}, success_expr="ok")],
    )


def test_registry_matches_by_symptoms(monkeypatch):
    """symptoms TF-IDF 命中：无 alertname 时也能召回。"""
    registry = SkillRegistry(skills_dir=None, include_legacy_playbooks=False)
    registry._skills = [
        _demo_skill("s.kafka", ["kafka lag", "消费积压", "消息堆积"]),
        _demo_skill("s.es", ["es red", "分片未分配"]),
    ]
    registry._rebuild_index()
    registry._metrics = {p.skill_id: registry._metrics.get(p.skill_id) for p in registry._skills}
    for pack in registry._skills:
        registry._metrics[pack.skill_id] = registry.__class__.__dict__["_load_internal"].__globals__["SkillMetric"]()
    registry._loaded = True

    matches = registry.match("Kafka 消费组积压严重，请排查")
    assert matches, "Kafka 关键词应命中 s.kafka"
    assert matches[0].skill.skill_id == "s.kafka"
    # 得分不为 0，理由包含 symptoms
    assert matches[0].score > 0.0
    assert any("symptoms" in reason for reason in matches[0].reasons)


def test_registry_alertname_gives_top_priority(monkeypatch):
    """alertname 精确命中：无 symptoms 也应压过纯语义候选。"""
    registry = SkillRegistry(skills_dir=None, include_legacy_playbooks=False)
    registry._skills = [
        _demo_skill("s.esred", symptoms=["集群 red"], alert="ElasticsearchClusterRed"),
        _demo_skill("s.kafka", symptoms=["kafka lag"]),
    ]
    registry._rebuild_index()
    from app.agent.skills.registry import SkillMetric
    registry._metrics = {p.skill_id: SkillMetric() for p in registry._skills}
    registry._loaded = True

    matches = registry.match(
        "无关的输入文本",
        alert_labels={"alertname": "ElasticsearchClusterRed"},
    )
    assert matches[0].skill.skill_id == "s.esred"
    assert any("alertname=" in reason for reason in matches[0].reasons)


def test_registry_disables_skill_with_missing_tools(monkeypatch):
    """required_tools 里出现不存在的工具时，Skill 被标 disabled_reason，不参与匹配。"""
    from app.agent import tool_registry as tr

    class _StubToolRegistry:
        catalog = {"prom_query": None}

    monkeypatch.setattr(tr, "tool_registry", _StubToolRegistry())

    good = _demo_skill("s.good", ["hello"], tool="prom_query")
    bad = SkillPack(
        skill_id="s.bad",
        title="缺工具",
        trigger=SkillTrigger(symptoms=["something"]),
        required_tools=["prom_query", "not_registered_tool"],
        steps=[SkillStep(id="s1", description="", tool_hint="prom_query")]
        if False
        else [SkillStep(id="s1", description="查", tool_hint="prom_query")],
    )
    registry = SkillRegistry(skills_dir=None, include_legacy_playbooks=False)
    registry._validate_against_tools([good, bad])

    assert good.disabled_reason is None
    assert bad.disabled_reason is not None
    assert "not_registered_tool" in bad.disabled_reason


def test_playbook_to_skill_normalizes_legacy(monkeypatch):
    """legacy playbook dict 能被 loader 转成 SkillPack，字段兜底安全。"""
    playbook = {
        "playbook_id": "pb-demo-kafka-lag",
        "title": "Kafka 消费组积压排障预案",
        "symptoms": ["kafka lag", "消费积压"],
        "steps": [
            {"description": "查询 lag 曲线", "tool_hint": "prom_query_range"},
            {"description": "对比生产消费速率", "tool_hint": "prom_query"},
        ],
        "root_cause_hint": "流量突增或消费者变慢",
    }
    pack = playbook_to_skill(playbook)
    assert pack is not None
    assert pack.skill_id == "pb-demo-kafka-lag"
    assert len(pack.steps) == 2
    assert pack.steps[0].tool_hint == "prom_query_range"
    assert pack.review_status == "demo"
    assert pack.source == "legacy-playbook"


def test_registry_records_activation_and_metrics(monkeypatch):
    registry = SkillRegistry(skills_dir=None, include_legacy_playbooks=False)
    registry._skills = [_demo_skill("s.foo", ["foo"])]
    registry._rebuild_index()
    from app.agent.skills.registry import SkillMetric
    registry._metrics = {"s.foo": SkillMetric()}
    registry._loaded = True

    registry.record_activation("s.foo")
    registry.record_activation("s.foo")
    registry.record_outcome("s.foo", "success")
    snapshot = registry.metrics_snapshot()
    entry = {row["skill_id"]: row for row in snapshot}["s.foo"]
    assert entry["activated"] == 2
    assert entry["succeeded"] == 1
    assert entry["success_rate"] == 0.5


def test_yaml_loader_parses_demo_skills():
    """内置 skills/aiops/*.yaml 能被轻量 YAML 解析器加载。"""
    root = Path(__file__).resolve().parents[1] / "skills" / "aiops"
    if not root.exists():
        return  # 允许 skills 目录未内置的部署
    from app.agent.skills.loader import load_skills_dir

    packs = load_skills_dir(root)
    ids = {p.skill_id for p in packs}
    assert "aiops.es_red" in ids
    assert "aiops.kafka_lag" in ids
    assert "aiops.db_pool" in ids
    for pack in packs:
        # 每个 Skill 都必须包含可执行 steps 与 verification（demo 场景）
        assert pack.steps, f"{pack.skill_id} 没有步骤"
        assert pack.verifications, f"{pack.skill_id} 缺少 verification"


def test_yaml_scalar_and_list_parsing():
    text = """
title: 测试
count: 3
enabled: true
labels:
  service: es
symptoms:
  - "es red"
  - '集群状态'
""".strip()
    parsed = parse_simple_yaml(text)
    assert parsed["title"] == "测试"
    assert parsed["count"] == 3
    assert parsed["enabled"] is True
    assert parsed["labels"] == {"service": "es"}
    assert parsed["symptoms"] == ["es red", "集群状态"]
