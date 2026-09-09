"""F4：LLMFactory 按场景配置 max_tokens"""

from pydantic import BaseModel

from app.config import config
from app.core.llm_factory import llm_factory, structured_output


class Plan(BaseModel):
    steps: list[str] = []


def test_create_chat_model_falls_back_to_config_default_max_tokens():
    """不传 max_tokens 时回落到 config.llm_max_tokens（对话等默认场景）"""
    llm = llm_factory.create_chat_model(model=config.rag_model, streaming=False)
    assert llm.max_tokens == config.llm_max_tokens


def test_create_chat_model_accepts_explicit_max_tokens():
    """显式传入 max_tokens 时按场景生效（规划/决策/报告等场景）"""
    for expected in (1024, 2048, 4096):
        llm = llm_factory.create_chat_model(
            model=config.rag_model, streaming=False, max_tokens=expected
        )
        assert llm.max_tokens == expected


def test_deepseek_uses_official_thinking_switch(monkeypatch):
    monkeypatch.setattr(config, "llm_provider", "deepseek")
    llm = llm_factory.create_chat_model(streaming=False, thinking=True)
    assert llm.extra_body == {"thinking": {"type": "enabled"}}


def test_deepseek_agent_mode_disables_thinking(monkeypatch):
    monkeypatch.setattr(config, "llm_provider", "deepseek")
    llm = llm_factory.create_chat_model(streaming=False)
    assert llm.extra_body == {"thinking": {"type": "disabled"}}


def test_structured_output_uses_function_calling_for_deepseek(monkeypatch):
    """DeepSeek 兼容端点拒绝 response_format(json_schema)，应走 function calling"""
    monkeypatch.setattr(config, "llm_provider", "deepseek")
    llm = llm_factory.create_chat_model(streaming=False)
    chain = structured_output(llm, Plan)
    assert "tool_choice" in chain.first.kwargs


def test_structured_output_keeps_default_for_other_providers(monkeypatch):
    monkeypatch.setattr(config, "llm_provider", "dashscope")
    monkeypatch.setattr(config, "dashscope_api_key", "test-key")
    llm = llm_factory.create_chat_model(streaming=False)
    chain = structured_output(llm, Plan)
    assert "response_format" in chain.first.kwargs
