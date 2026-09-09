import pytest
from langchain_core.messages import AIMessage

from app.services.rag_agent_service import RagAgentService


def test_retrieval_fallback_is_explicit_and_bounded() -> None:
    answer = RagAgentService._format_retrieval_fallback("CPU 排查证据" * 1000)

    assert answer.startswith("模型服务暂不可用")
    assert "尚未经过 LLM 归纳" in answer
    assert len(answer) < 4100


@pytest.mark.parametrize("context", ["", "没有找到相关信息。", "检索知识时发生错误: timeout"])
def test_retrieval_fallback_rejects_missing_evidence(context: str) -> None:
    with pytest.raises(RuntimeError):
        RagAgentService._format_retrieval_fallback(context)


@pytest.mark.asyncio
async def test_summary_retries_one_transient_connection_error(monkeypatch) -> None:
    service = RagAgentService.__new__(RagAgentService)
    calls = 0

    class Model:
        async def ainvoke(self, messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("temporary connection error")
            return AIMessage(content="归纳成功")

    service.model = Model()

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("app.services.rag_agent_service.asyncio.sleep", no_wait)
    result = await service._invoke_summary_with_retry([])
    assert result.content == "归纳成功"
    assert calls == 2


@pytest.mark.asyncio
async def test_explicit_read_only_tool_fallback_does_not_use_rag() -> None:
    """显式点名的只读工具可在模型不可用时直接执行（资格来自注册中心元数据）。"""
    service = RagAgentService.__new__(RagAgentService)

    class Tool:
        name = "prom_active_alerts"

        async def ainvoke(self, arguments):
            assert arguments == {}
            return {"alerts": [], "mode": "read_only"}

    service.mcp_tools = [Tool()]
    result = await service._explicit_read_only_tool_fallback("请使用 prom_active_alerts 检查")
    assert result is not None
    assert "只读工具" in result
    assert "prom_active_alerts" in result


@pytest.mark.asyncio
async def test_fallback_refuses_tools_that_are_not_registered_read_only() -> None:
    """未注册工具按保守缺省视为不安全，不得在无模型兜底路径里被直接执行。"""
    service = RagAgentService.__new__(RagAgentService)

    class Tool:
        name = "unregistered_write_tool"

        async def ainvoke(self, arguments):  # pragma: no cover - must not run
            raise AssertionError("unsafe tool must not be invoked")

    service.mcp_tools = [Tool()]
    result = await service._explicit_read_only_tool_fallback(
        "请使用 unregistered_write_tool 处理"
    )
    assert result is None
