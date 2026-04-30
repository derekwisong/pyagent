"""LLM provider clients and the interface they share.

Adding a provider: append one entry to `PROVIDERS`. The same registry
drives `get_client()` dispatch and env-var auto-detection — there is no
second place to update.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's registration data.

    Attributes:
        name: Provider identifier used in `--model` ("anthropic", "openai", ...).
        env_vars: API-key environment variables, in the order the client
            checks them. The first one set is enough to auto-detect this
            provider. Empty tuple = no env requirement (e.g. local stub).
        default_model: Concrete model name the client picks when no model
            is specified. Mirrors the client's own constructor default; kept
            here so callers (e.g. the session-header printer) can format
            the resolved string without instantiating the client.
        factory: Callable that takes optional `model=` and returns a client.
            The actual SDK import happens here, so unused providers don't
            pay the import cost.
    """

    name: str
    env_vars: tuple[str, ...]
    default_model: str
    factory: Callable[..., "LLMClient"]


def _anthropic_factory(**kw: Any) -> "LLMClient":
    from pyagent.llms.anthropic import AnthropicClient

    return AnthropicClient(**kw)


def _openai_factory(**kw: Any) -> "LLMClient":
    from pyagent.llms.openai import OpenAIClient

    return OpenAIClient(**kw)


def _gemini_factory(**kw: Any) -> "LLMClient":
    from pyagent.llms.gemini import GeminiClient

    return GeminiClient(**kw)


def _pyagent_factory(**kw: Any) -> "LLMClient":
    from pyagent.llms.pyagent import EchoClient, LoremClient

    name = kw.get("model")
    stubs = {"echo": EchoClient, "loremipsum": LoremClient}
    cls = stubs.get(name) if name else EchoClient
    if cls is None:
        raise ValueError(
            f"Unknown pyagent stub {name!r} (expected: {sorted(stubs)})"
        )
    return cls() if not name else cls(model=name)


# Order matters: auto-detection picks the first provider whose env_vars
# are satisfied. Real providers come before the local stub so a user
# with a real API key never gets the echo stub by accident.
PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(
        name="anthropic",
        env_vars=("ANTHROPIC_API_KEY",),
        default_model="claude-sonnet-4-6",
        factory=_anthropic_factory,
    ),
    ProviderSpec(
        name="openai",
        env_vars=("OPENAI_API_KEY",),
        default_model="gpt-4o",
        factory=_openai_factory,
    ),
    ProviderSpec(
        name="gemini",
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_model="gemini-2.5-flash",
        factory=_gemini_factory,
    ),
    ProviderSpec(
        name="pyagent",
        env_vars=(),
        default_model="echo",
        factory=_pyagent_factory,
    ),
]


def _by_name(name: str) -> ProviderSpec | None:
    for p in PROVIDERS:
        if p.name == name:
            return p
    return None


def get_client(model: str) -> "LLMClient":
    """Resolve a "provider/model" string to a concrete LLMClient instance.

    Examples:
        get_client("anthropic")                     # AnthropicClient defaults
        get_client("anthropic/claude-opus-4-7")     # explicit model
        get_client("openai/gpt-4o")
        get_client("gemini/gemini-2.5-flash")
        get_client("pyagent/echo")                  # local stub, no API call

    Raises:
        ValueError: If the provider is unknown.
    """
    provider, _, name = model.partition("/")
    spec = _by_name(provider)
    if spec is None:
        known = ", ".join(p.name for p in PROVIDERS)
        raise ValueError(f"Unknown provider {provider!r} (expected: {known})")
    kwargs = {"model": name} if name else {}
    return spec.factory(**kwargs)


def resolve_model(model: str) -> str:
    """Return 'provider/model-name' with the bundled default filled in
    for any unspecified part. No SDK imports, no client construction.

    Unknown providers are returned unchanged so `get_client` can raise.
    """
    provider, _, name = model.partition("/")
    spec = _by_name(provider)
    if spec is None:
        return model
    return f"{provider}/{name or spec.default_model}"


def auto_detect_provider() -> ProviderSpec | None:
    """Return the first provider whose env_vars are satisfied in the
    current process environment, or None if none are.

    Providers with no env_vars (local stubs) are skipped — they would
    always match and would shadow real providers.
    """
    for spec in PROVIDERS:
        if not spec.env_vars:
            continue
        if any(os.environ.get(v) for v in spec.env_vars):
            return spec
    return None


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
        system_volatile: str | None = None,
    ) -> dict[str, Any]: ...
