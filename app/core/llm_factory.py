"""Create OpenAI-compatible chat models without leaking provider-specific options."""

from langchain_openai import ChatOpenAI

from app.config import config


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
        )


llm_factory = LLMFactory()
