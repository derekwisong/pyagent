"""LLM provider clients and the interface they share."""

from typing import Any, Protocol


def get_client(model: str) -> "LLMClient":
    """Resolve a "provider/model" string to a concrete LLMClient instance.

    The model name is optional; when omitted, each client's own default model
    is used.

    Examples:
        get_client("anthropic")                     # AnthropicClient defaults
        get_client("anthropic/claude-opus-4-7")     # explicit model
        get_client("openai/gpt-4o")
        get_client("gemini/gemini-2.5-flash")
        get_client("pyagent/echo")                  # local stub, no API call

    Args:
        model: Provider name, optionally followed by "/model-name". Provider
            must be one of "anthropic", "openai", "gemini", or "pyagent".

    Returns:
        A client instance for the requested provider.

    Raises:
        ValueError: If the provider is unknown.
    """
    provider, _, name = model.partition("/")
    kwargs = {"model": name} if name else {}

    if provider == "anthropic":
        from pyagent.llms.anthropic import AnthropicClient

        return AnthropicClient(**kwargs)
    if provider == "openai":
        from pyagent.llms.openai import OpenAIClient

        return OpenAIClient(**kwargs)
    if provider == "gemini":
        from pyagent.llms.gemini import GeminiClient

        return GeminiClient(**kwargs)
    if provider == "pyagent":
        from pyagent.llms.pyagent import EchoClient, LoremClient

        stubs = {"echo": EchoClient, "loremipsum": LoremClient}
        cls = stubs.get(name) if name else EchoClient
        if cls is None:
            raise ValueError(
                f"Unknown pyagent stub {name!r} "
                f"(expected: {sorted(stubs)})"
            )
        return cls() if not name else cls(model=name)

    raise ValueError(
        f"Unknown provider {provider!r} "
        "(expected anthropic, openai, gemini, pyagent)"
    )


class LLMClient(Protocol):
    """One-turn interface that every provider client implements.

    The conversation is a list of messages in our internal format:

      - User text:    {"role": "user", "content": "..."}
      - Tool results: {"role": "user", "tool_results": [{"id", "name", "content"}, ...]}
      - Assistant:    {"role": "assistant", "text": "...",
                       "tool_calls": [{"id", "name", "args"}, ...]}

    The return value is a single assistant message in that same shape.
    """

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]: ...
