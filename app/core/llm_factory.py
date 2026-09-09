"""Create OpenAI-compatible chat models without leaking provider-specific options.

需要 ``[llm]`` extra（``langchain-openai``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

try:
    from langchain_openai import ChatOpenAI
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "创建 LLM 客户端需要 `[llm]` extra。"
        "\n    pip install 'aiops-copilot[llm]'"
    ) from _exc

from app.config import config
from app.observability.llm_metrics import LLMMetricsCallback


class LLMFactory:
    """Centralized model construction for Ollama, DashScope and DeepSeek."""

    DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEEPSEEK_BASE_URL = "https://api.deepseek.com"

    @staticmethod
    def create_chat_model(
        model: str | None = None,
        temperature: float = 0.7,
        streaming: bool = True,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        *,
        thinking: bool = False,
    ) -> ChatOpenAI:
        """Build a model with options valid for the selected provider.

        DeepSeek V4 uses an explicit ``thinking`` switch. Agent tool loops default
        to non-thinking mode because reasoning_content must otherwise be preserved
        across every tool turn. Thinking mode is opt-in for bounded report tasks.
        """
        provider = config.llm_provider.strip().lower()

        if provider == "dashscope":
            selected_model = model or config.dashscope_model
            selected_base_url = base_url or config.dashscope_api_base
            selected_api_key = api_key or config.dashscope_api_key
            extra_body = None
        else:
            selected_model = model or config.llm_model
            selected_base_url = base_url or config.llm_api_base
            selected_api_key = api_key or config.llm_api_key
            if provider == "ollama":
                extra_body = {"think": thinking}
            elif provider == "deepseek":
                extra_body = {"thinking": {"type": "enabled" if thinking else "disabled"}}
            else:
                extra_body = None

        return ChatOpenAI(
            model=selected_model,
            temperature=temperature,
            streaming=streaming,
            base_url=selected_base_url,
            api_key=selected_api_key,
            extra_body=extra_body,
            max_tokens=max_tokens if max_tokens is not None else config.llm_max_tokens,
            timeout=config.llm_timeout_seconds,
            max_retries=config.llm_max_retries,
            # Model-level callbacks are inherited by bind_tools/with_structured_output
            # derived runnables, so a single handler covers every derived chain.
            callbacks=[LLMMetricsCallback(selected_model)],
        )


llm_factory = LLMFactory()


def structured_output(llm: ChatOpenAI, schema):
    """Structured output with a method the configured provider actually supports.

    DeepSeek's OpenAI-compatible endpoint rejects ``response_format``-based
    methods (``json_schema`` / ``json_object``) with HTTP 400, so route through
    function calling, which it does support. Other providers keep the default.
    """
    if config.llm_provider.strip().lower() == "deepseek":
        return llm.with_structured_output(schema, method="function_calling")
    return llm.with_structured_output(schema)
