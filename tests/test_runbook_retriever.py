"""P2-1：Skill 显式声明的 runbook_refs → RunbookRetriever 按 h2 抽取 → 拼进 Executor prompt。

覆盖：
- RunbookRetriever 按 doc_id 加载 aiops-docs/*.md 并按 h2 切
- extract_relevant 按 query 打分排序，取 top_k，命中标题加权
- doc_id 不存在时返回空
- mtime 缓存生效（同一 mtime 只读盘一次）
- executor._fetch_runbook_hint 拼进 skill.runbook_refs 声明的 wiki 内容
- SkillPack.runbook_refs 字段默认空、YAML 加载后能读到
"""

from __future__ import annotations

from pathlib import Path

from app.agent.aiops.executor import _fetch_runbook_hint
from app.agent.skills.models import (
    SkillPack,
    SkillStep,
    SkillTrigger,
)
from app.agent.skills.runbook_retriever import (
    RunbookRetriever,
    RunbookSection,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = PROJECT_ROOT / "aiops-docs"


# ---------------- RunbookRetriever ----------------


def test_retriever_loads_h2_sections_from_real_wiki():
    """真实 wiki 文档：kafka_consumer_lag.md 至少应有 3 个 h2 小节。"""
    r = RunbookRetriever(DOCS_DIR)
    sections = r.load_sections("kafka_consumer_lag")
    assert len(sections) >= 3
    titles = [s.section_title for s in sections]
    # runbook 骨架里常见的 h2 之一
    assert any(t in {"识别信号", "排查顺序", "常见根因", "快速缓解", "验证清单"} for t in titles)
    for s in sections:
        assert s.doc_id == "kafka_consumer_lag"
        assert s.content.startswith("## ")


def test_retriever_missing_doc_returns_empty():
    r = RunbookRetriever(DOCS_DIR)
    assert r.load_sections("this_doc_does_not_exist") == []
    # 不存在的 doc 也不会污染 extract_relevant
    assert r.extract_relevant(["nope"], "query") == []


def test_retriever_extract_relevant_ranks_by_score():
    """query 命中「快速缓解」小节标题时应被排到前面。"""
    r = RunbookRetriever(DOCS_DIR)
    top = r.extract_relevant(
        ["kafka_consumer_lag"], "如何快速缓解 Kafka lag 积压", top_k=3
    )
    assert len(top) >= 1
    # 标题命中会 +2 权重，快速缓解应命中
    assert any("缓解" in s.section_title or "缓解" in s.content for s in top)


def test_retriever_extract_relevant_truncates_long_sections():
    r = RunbookRetriever(DOCS_DIR)
    top = r.extract_relevant(
        ["kafka_consumer_lag"], "排查", top_k=1, max_chars_per_section=100
    )
    assert len(top) == 1
    assert len(top[0].content) <= 100 + len("……[已截断]")


def test_retriever_mtime_cache_avoids_reread(tmp_path, monkeypatch):
    """相同 mtime 时 load_sections 不重复读盘。"""
    doc = tmp_path / "sample.md"
    doc.write_text(
        "# Sample\n\n## Section A\n\nabc\n\n## Section B\n\ndef\n",
        encoding="utf-8",
    )
    r = RunbookRetriever(tmp_path)
    first = r.load_sections("sample")
    # 打桩 read_text 使二次调用炸掉；若走缓存则不会被触发
    call_count = {"n": 0}
    original_read = Path.read_text

    def _traced_read(self, *args, **kwargs):
        if self == doc:
            call_count["n"] += 1
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _traced_read)
    second = r.load_sections("sample")
    assert second == first
    assert call_count["n"] == 0  # 缓存命中，未重新读


def test_retriever_extract_empty_query_returns_empty():
    r = RunbookRetriever(DOCS_DIR)
    assert r.extract_relevant(["kafka_consumer_lag"], "") == []


def test_runbook_section_prompt_block_format():
    s = RunbookSection(
        doc_id="kafka_consumer_lag",
        doc_title="Kafka Consumer Lag 处理方案",
        section_title="快速缓解",
        content="## 快速缓解\n\n扩容 consumer 分区数。",
        file_path="/dummy",
    )
    block = s.as_prompt_block()
    assert "Kafka Consumer Lag 处理方案" in block
    assert "快速缓解" in block
    assert "aiops-docs/kafka_consumer_lag.md" in block


# ---------------- SkillPack.runbook_refs ----------------


def _make_pack(runbook_refs: list[str]) -> SkillPack:
    return SkillPack(
        skill_id="test.pack",
        title="test",
        trigger=SkillTrigger(),
        steps=[SkillStep(id="s1", description="do something")],
        runbook_refs=runbook_refs,
    )


def test_skill_pack_runbook_refs_defaults_to_empty():
    pack = SkillPack(
        skill_id="t",
        title="t",
        trigger=SkillTrigger(),
        steps=[SkillStep(id="s1", description="x")],
    )
    assert pack.runbook_refs == []


def test_skill_pack_runbook_refs_populates_from_kwarg():
    pack = _make_pack(["kafka_consumer_lag", "kafka_rebalance"])
    assert pack.runbook_refs == ["kafka_consumer_lag", "kafka_rebalance"]


def test_builtin_yaml_skills_declare_runbook_refs():
    """本次改造：三个内置 skill YAML 都应声明至少一个 runbook_ref。"""
    from app.agent.skills.loader import load_skills_dir

    packs = load_skills_dir(PROJECT_ROOT / "skills")
    yaml_packs = {p.skill_id: p for p in packs if "yaml" in (p.source or "")}
    for skill_id in ("aiops.db_pool", "aiops.es_red", "aiops.kafka_lag"):
        assert skill_id in yaml_packs, f"missing skill {skill_id}"
        assert len(yaml_packs[skill_id].runbook_refs) >= 1


# ---------------- executor._fetch_runbook_hint ----------------


def test_fetch_hint_returns_empty_without_skill_id():
    assert _fetch_runbook_hint("", "任何描述") == ""


def test_fetch_hint_returns_empty_without_step_desc():
    assert _fetch_runbook_hint("some.skill", "") == ""


def test_fetch_hint_returns_empty_when_skill_unknown(monkeypatch):
    from app.agent.skills import registry as reg_module

    # 打桩 registry.get 使其返回 None
    class _FakeReg:
        def get(self, sid):
            return None

    monkeypatch.setattr(reg_module, "_default_registry", _FakeReg())
    monkeypatch.setattr(reg_module, "get_skill_registry", lambda: _FakeReg())
    assert _fetch_runbook_hint("unknown.skill", "查询连接数") == ""


def test_fetch_hint_returns_empty_when_skill_has_no_runbook_refs(monkeypatch):
    from app.agent.skills import registry as reg_module

    pack = _make_pack([])

    class _Reg:
        def get(self, sid):
            return pack if sid == pack.skill_id else None

    monkeypatch.setattr(reg_module, "get_skill_registry", lambda: _Reg())
    assert _fetch_runbook_hint(pack.skill_id, "查询") == ""


def test_fetch_hint_pulls_from_real_wiki_for_kafka_skill(monkeypatch):
    """端到端：真实 kafka skill + 真实 wiki 文件 → 拼出参考段落。"""
    from app.agent.skills import registry as reg_module

    pack = _make_pack(["kafka_consumer_lag"])
    pack = pack.model_copy(update={"skill_id": "test.kafka"})

    class _Reg:
        def get(self, sid):
            return pack if sid == "test.kafka" else None

    monkeypatch.setattr(reg_module, "get_skill_registry", lambda: _Reg())
    hint = _fetch_runbook_hint("test.kafka", "如何快速缓解消费者积压")
    assert hint
    assert "kafka_consumer_lag" in hint or "Kafka Consumer Lag" in hint
    assert "【参考：" in hint
