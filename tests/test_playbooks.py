"""P2.4：预案库（程序性记忆）"""

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.main import app
from app.services.playbook_service import (
    PlaybookService,
)

planner_module = importlib.import_module("app.agent.aiops.planner")
client = TestClient(app)


@pytest.fixture
def service():
    return PlaybookService()  # 无路径 → 内置演示预案


def test_builtin_demo_playbooks_load_with_source(service):
    playbooks = service.list_playbooks()
    assert service.source == "builtin-demo"
    assert len(playbooks) == 3
    assert all(pb.get("review_status") == "demo" for pb in playbooks)


def test_match_by_symptom(service):
    hits = service.match("Kafka 消费组积压严重，消息堆积处理不过来")
    assert hits, "Kafka 症状应命中消费积压预案"
    assert hits[0]["playbook_id"] == "pb-demo-kafka-lag"
    assert hits[0]["score"] > 0

    es_hits = service.match("es 集群状态 red，分片未分配")
    assert es_hits[0]["playbook_id"] == "pb-demo-es-red"


def test_match_ignores_unrelated(service):
    assert service.match("前端页面样式错乱") == []
    assert service.match("") == []


def test_format_block_contains_steps_and_verification(service):
    block = service.format_matched_playbooks("数据库连接池耗尽，应用获取连接超时")
    assert "匹配预案" in block
    assert "连接池耗尽" in block
    assert "mysql_read_query" in block  # 步骤工具提示
    assert "验证标准" in block
    assert "必须用工具验证" in block  # 防止盲套结论的警示


def test_custom_playbook_file_overrides_builtin(tmp_path):
    path = tmp_path / "playbooks.jsonl"
    row = {
        "playbook_id": "pb-custom-1",
        "title": "自定义磁盘满预案",
        "symptoms": ["磁盘满", "disk full", "inode 耗尽"],
        "steps": [{"description": "查磁盘与 inode 使用率", "tool_hint": "prom_query"}],
        "root_cause_hint": "日志未轮转",
        "review_status": "approved",
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\nbad-json-line\n", encoding="utf-8")
    service = PlaybookService(path=str(path))
    playbooks = service.list_playbooks()  # 触发惰性加载
    assert service.source == "file:playbooks.jsonl"
    assert len(playbooks) == 1  # 坏行跳过
    assert service.match("磁盘满了 inode 耗尽")[0]["playbook_id"] == "pb-custom-1"


def test_broken_configured_path_yields_empty_library(tmp_path):
    service = PlaybookService(path=str(tmp_path / "missing.jsonl"))
    assert service.list_playbooks() == []
    assert service.match("任何症状") == []


def test_reload_picks_up_changes(tmp_path):
    path = tmp_path / "playbooks.jsonl"
    path.write_text(json.dumps({"playbook_id": "pb-1", "title": "预案一", "symptoms": ["alpha"]}), encoding="utf-8")
    service = PlaybookService(path=str(path))
    assert service.match("alpha 场景")
    path.write_text(json.dumps({"playbook_id": "pb-2", "title": "预案二", "symptoms": ["beta"]}), encoding="utf-8")
    service.reload()
    assert not service.match("alpha 场景")
    assert service.match("beta 场景")


# ---- Planner 注入 ----


async def test_planner_injects_matched_playbook(monkeypatch, tmp_path):
    """Planner 上下文同时包含 runbook、事件记忆与匹配预案（三层记忆齐备）"""

    class _FakeRetrieveTool:
        name = "retrieve_knowledge"
        description = "检索知识库"

        async def ainvoke(self, args):  # noqa: ARG002
            return "runbook: 连接池耗尽处理标准"

    class _FakeMCPClient:
        async def get_tools(self):
            return []

    async def _fake_mcp_client():
        return _FakeMCPClient()

    captured: list[str] = []

    class _PlainLLM:
        def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
            from langchain_core.runnables import RunnableLambda

            async def _fail(_input):
                raise RuntimeError("不支持")

            return RunnableLambda(_fail)

        async def ainvoke(self, messages):
            from langchain_core.messages import AIMessage

            captured.append("\n".join(str(m.content) for m in messages))
            return AIMessage(content="步骤1: 排查连接池\n步骤2: 生成报告")

    # 事件记忆：沉淀一条相似历史
    monkeypatch.setattr(config, "aiops_incident_memory_enabled", True)
    monkeypatch.setattr(config, "aiops_incident_memory_path", str(tmp_path / "m.jsonl"))
    from app.services.incident_memory_service import IncidentMemoryService

    memory = IncidentMemoryService()
    await memory.record_episode(
        input_text="数据库连接池耗尽告警",
        past_steps=[("查连接数", "1200/1210")],
        response="根因是慢查询堆积导致连接耗尽",
    )
    monkeypatch.setattr(planner_module, "incident_memory_service", memory)

    # 预案库：使用内置演示（连接池预案应命中）
    monkeypatch.setattr(config, "aiops_playbooks_path", "")
    import app.services.playbook_service as pb_module

    monkeypatch.setattr(pb_module, "playbook_service", None)

    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(
        planner_module.llm_factory, "create_chat_model", lambda **kwargs: _PlainLLM()
    )
    # 本用例验证 legacy playbook 文本注入 LLM prompt；禁用 Skill 的确定性短路。
    import app.agent.skills as skills_pkg

    class _NoSkillRegistry:
        def match(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(skills_pkg, "get_skill_registry", lambda: _NoSkillRegistry())

    state = {
        "input": "数据库连接池耗尽，服务大量报错",
        "plan": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    result = await planner_module.planner(state)

    assert result["degraded"] is False
    prompt_text = captured[0]
    assert "相关经验文档" in prompt_text
    assert "历史相似事件" in prompt_text
    assert "匹配预案" in prompt_text
    assert "连接池耗尽" in prompt_text


# ---- HTTP API ----


def test_playbooks_api_listing_and_match():
    listing = client.get("/api/playbooks")
    assert listing.status_code == 200
    body = listing.json()
    assert body["source"] == "builtin-demo"
    assert body["count"] == 3

    matched = client.get("/api/playbooks/match", params={"q": "kafka 消费积压"})
    assert matched.status_code == 200
    ids = [item["playbook_id"] for item in matched.json()["matched"]]
    assert "pb-demo-kafka-lag" in ids

    assert client.get("/api/playbooks/match", params={"q": ""}).status_code == 422
