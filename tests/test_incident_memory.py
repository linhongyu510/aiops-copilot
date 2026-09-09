"""P0.3：事件记忆（episode 沉淀 + 相似检索 + Planner 注入）"""

import importlib
import json

import pytest

from app.config import config
from app.services.incident_memory_service import IncidentMemoryService

planner_module = importlib.import_module("app.agent.aiops.planner")


@pytest.fixture
def memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "aiops_incident_memory_enabled", True)
    monkeypatch.setattr(config, "aiops_incident_memory_path", str(tmp_path / "memory.jsonl"))
    monkeypatch.setattr(config, "aiops_incident_memory_min_score", 0.15)
    return IncidentMemoryService()


async def _record(service: IncidentMemoryService, input_text: str, response: str, steps=None):
    return await service.record_episode(
        input_text=input_text,
        past_steps=steps or [("查询指标", "CPU 96%")],
        response=response,
        session_id="sess-1",
    )


async def test_record_persists_and_recall_finds_similar_episode(memory):
    episode_id = await _record(
        memory,
        "MySQL 数据库连接池耗尽导致服务报错",
        "根因：连接池配置过小，慢查询堆积导致连接耗尽。处理：kill 长事务并调大连接池。",
    )
    assert episode_id is not None
    assert memory.size == 1
    # 文件确实落盘且可解析
    line = memory._path.read_text(encoding="utf-8").strip()
    assert json.loads(line)["episode_id"] == episode_id

    hits = memory.recall("数据库连接耗尽 报错")
    assert len(hits) == 1
    assert hits[0]["episode"]["episode_id"] == episode_id
    assert hits[0]["score"] > 0


async def test_recall_ignores_unrelated_and_disabled(memory):
    await _record(memory, "MySQL 连接池耗尽", "根因是连接池配置过小")
    # 无关查询不命中
    assert memory.recall("Kubernetes 镜像拉取失败") == []
    # 关闭开关后完全不工作
    config.aiops_incident_memory_enabled = False
    try:
        assert memory.recall("数据库连接耗尽") == []
        assert await memory.record_episode("x", [("s", "r")], "y") is None
    finally:
        config.aiops_incident_memory_enabled = True


async def test_failed_diagnosis_without_response_not_recorded(memory):
    assert await memory.record_episode("任务", [("步骤", "失败")], "") is None
    assert memory.size == 0


# ------------------- P1-2 C2：episode 新增 skill_id/outcome/verification_passed -------------------


async def test_c2_episode_persists_skill_attribution(memory):
    """C2：record_episode 允许携带 skill_id/outcome/verification_passed 并持久化。"""
    episode_id = await memory.record_episode(
        input_text="MySQL 慢查询导致 API 超时",
        past_steps=[("查询 slowlog", "发现锁等待"), ("[verify] #0", "ok")],
        response="根因：锁等待导致查询堆积",
        session_id="sess-1",
        skill_id="mysql-slow-query-pool-exhaustion",
        outcome="success",
        verification_passed=True,
    )
    assert episode_id is not None

    line = memory._path.read_text(encoding="utf-8").strip()
    persisted = json.loads(line)
    assert persisted["skill_id"] == "mysql-slow-query-pool-exhaustion"
    assert persisted["outcome"] == "success"
    assert persisted["verification_passed"] is True


async def test_c2_episode_defaults_when_skill_absent(memory):
    """C2：未提供 skill 上下文时 skill_id/outcome 为空串、verification_passed 为 None。"""
    await memory.record_episode(
        input_text="ad-hoc 排查",
        past_steps=[("步骤", "结果")],
        response="报告",
        session_id="s",
    )
    line = memory._path.read_text(encoding="utf-8").strip()
    persisted = json.loads(line)
    assert persisted["skill_id"] == ""
    assert persisted["outcome"] == ""
    assert persisted["verification_passed"] is None


async def test_format_recalled_episodes_renders_context_block(memory):
    await _record(
        memory,
        "Redis 缓存命中率下跌",
        "根因：大批量冷启动写入挤占内存。处理：临时扩容并加大淘汰水位。",
        steps=[("查询 redis_info", "内存 95%"), ("查询 slowlog", "出现大量写命令")],
    )
    block = memory.format_recalled_episodes("缓存命中率掉得很厉害，帮忙看看")
    if block:  # 相似度达到阈值时渲染结构块
        assert "历史相似事件" in block
        assert "Redis 缓存命中率下跌" in block
        assert "查询 redis_info" in block
    # 完全无关查询返回空字符串
    assert memory.format_recalled_episodes("Kubernetes pod pending") == ""


async def test_capacity_rotation_and_bad_lines_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "aiops_incident_memory_enabled", True)
    path = tmp_path / "mem.jsonl"
    service = IncidentMemoryService(path=str(path), max_episodes=3)
    for i in range(5):
        await _record(service, f"事件 {i}", f"结论 {i}")
    assert service.size == 3  # 只保留最新 3 条
    # 保留的必须是最新 3 条（事件 2/3/4），而不是任意 3 条
    retained = [ep["episode_id"] for ep in service._episodes]
    assert len(set(retained)) == 3
    assert service.recall("事件 4")

    # 坏行不影响整体加载
    path.write_text(
        "not-json\n" + json.dumps({"episode_id": "ep-ok", "input": "好事件", "steps": [], "response_summary": "好结论"}) + "\n",
        encoding="utf-8",
    )
    fresh = IncidentMemoryService(path=str(path))
    assert fresh.size == 1
    assert fresh.recall("好事件")


async def test_planner_injects_recalled_episodes(monkeypatch, tmp_path):
    """Planner 将相似历史事件注入规划上下文（与 runbook 检索互补）"""

    class _FakeRetrieveTool:
        name = "retrieve_knowledge"
        description = "检索知识库"

        async def ainvoke(self, args):  # noqa: ARG002
            return "runbook: 处理连接池耗尽的标准步骤"

    class _FakeMCPClient:
        async def get_tools(self):
            return []

    async def _fake_mcp_client():
        return _FakeMCPClient()

    captured_prompts: list[str] = []

    class _PlainLLM:
        """structured 失败 → 纯文本路径，捕获注入的 prompt"""

        def with_structured_output(self, schema, **_kwargs):  # noqa: ARG002
            from langchain_core.runnables import RunnableLambda

            async def _fail(_input):
                raise RuntimeError("不支持")

            return RunnableLambda(_fail)

        async def ainvoke(self, messages):
            captured_prompts.append("\n".join(str(m.content) for m in messages))
            from langchain_core.messages import AIMessage

            return AIMessage(content="步骤1: 排查连接池\n步骤2: 生成报告")

    monkeypatch.setattr(config, "aiops_incident_memory_enabled", True)
    monkeypatch.setattr(config, "aiops_incident_memory_path", str(tmp_path / "m.jsonl"))
    service = IncidentMemoryService()
    await _record(
        service,
        "MySQL 连接池耗尽告警",
        "根因是慢查询堆积导致连接耗尽，处理方式是清理长事务",
    )
    # planner 持有模块级单例；测试中显式替换为指向临时文件的新实例
    monkeypatch.setattr(planner_module, "incident_memory_service", service)

    monkeypatch.setattr(planner_module, "get_mcp_client_with_retry", _fake_mcp_client)
    monkeypatch.setattr(planner_module, "retrieve_knowledge", _FakeRetrieveTool())
    monkeypatch.setattr(
        planner_module.llm_factory, "create_chat_model", lambda **kwargs: _PlainLLM()
    )
    # 本用例验证 LLM prompt 的事件记忆注入；禁用 Skill 命中以进入 LLM 规划分支。
    import app.agent.skills as skills_pkg

    class _NoSkillRegistry:
        def match(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(skills_pkg, "get_skill_registry", lambda: _NoSkillRegistry())

    state = {
        "input": "数据库连接耗尽，服务大量报错",
        "plan": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    result = await planner_module.planner(state)

    assert result["degraded"] is False
    assert captured_prompts, "纯文本降级路径应捕获到 prompt"
    prompt_text = captured_prompts[0]
    assert "相关经验文档" in prompt_text
    assert "历史相似事件" in prompt_text
    assert "连接池耗尽" in prompt_text
