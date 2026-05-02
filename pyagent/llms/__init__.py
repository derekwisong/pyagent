"""LLM provider clients and the interface they share.

Adding a *built-in* provider: append one entry to `PROVIDERS`. The
same registry drives `get_client()` dispatch and env-var
auto-detection — there is no second place to update.

Plugins can also contribute providers via `PluginAPI.register_provider`
(see `pyagent.plugins`). The plugin loader publishes them here through
`set_plugin_providers` so `get_client("<plugin-provider>/<model>")`
resolves alongside built-ins. Plugin providers are deliberately
excluded from `auto_detect_provider` — they're opt-in via `--model`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class ModelInfo:
    """One model the provider can serve, plus optional capability tags.

    `name` is the string callers pass after `provider/` in `--model`.
    `capabilities` is a free-form tuple of tags the provider chose to
    surface (e.g. ``"tools"``, ``"vision"``, ``"embedding"``) — used
    by the CLI listing to flag models that are or aren't compatible
    with pyagent's tool-using agent loop. Empty tuple means "the
    provider didn't enumerate"; the CLI prints nothing rather than
    over-claiming. We deliberately don't enumerate capabilities for
    built-ins (anthropic / openai / gemini): that data is meaningful
    only when it can vary per model, which is the ollama story.
    """

    name: str
    capabilities: tuple[str, ...] = ()


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
        list_models: Optional callable returning `ModelInfo` records
            for the models this provider can serve. Built-in providers
            return a hardcoded canonical list — no network, no API
            key required, and capabilities are left empty (those
            providers' models are uniformly tool-capable). Live
            providers like ollama populate capabilities from the
            backend (`/api/show`) so the CLI can flag tool/vision/
            embedding variants. Callable may raise to signal an
            unreachable backend; the aggregator catches per-provider.
    """

    name: str
    env_vars: tuple[str, ...]
    default_model: str
    factory: Callable[..., "LLMClient"]
    list_models: Callable[[], list[ModelInfo]] | None = None


# Canonical recent-and-popular model lists for each built-in. Hardcoded
# rather than queried so `pyagent --list-models` works without API
# keys, instantly. Bumping these is a one-line edit when a new model
# ships; the alternative (live `/v1/models` calls) silently fails for
# users without keys configured.
def _anthropic_models() -> list[ModelInfo]:
    return [
        ModelInfo(name="claude-opus-4-7"),
        ModelInfo(name="claude-sonnet-4-6"),
        ModelInfo(name="claude-haiku-4-5-20251001"),
    ]


def _openai_models() -> list[ModelInfo]:
    return [
        ModelInfo(name="gpt-4o"),
        ModelInfo(name="gpt-4o-mini"),
        ModelInfo(name="o1"),
        ModelInfo(name="o1-mini"),
        ModelInfo(name="o3-mini"),
    ]


def _gemini_models() -> list[ModelInfo]:
    return [
        ModelInfo(name="gemini-2.5-flash"),
        ModelInfo(name="gemini-2.5-pro"),
        ModelInfo(name="gemini-2.0-flash"),
    ]


def _pyagent_models() -> list[ModelInfo]:
    return [ModelInfo(name="echo"), ModelInfo(name="loremipsum")]


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
        list_models=_anthropic_models,
    ),
    ProviderSpec(
        name="openai",
        env_vars=("OPENAI_API_KEY",),
        default_model="gpt-4o",
        factory=_openai_factory,
        list_models=_openai_models,
    ),
    ProviderSpec(
        name="gemini",
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        default_model="gemini-2.5-flash",
        factory=_gemini_factory,
        list_models=_gemini_models,
    ),
    ProviderSpec(
        name="pyagent",
        env_vars=(),
        default_model="echo",
        factory=_pyagent_factory,
        list_models=_pyagent_models,
    ),
]


# Plugin-registered providers. Populated by `set_plugin_providers`,
# which the plugin loader calls at the end of `plugins.load()`. Kept as
# module state (rather than a parameter on every call site) because
# `get_client` / `resolve_model` are scattered through the codebase
# and threading a registry argument through all of them would be
# invasive for a feature only a few callers need to think about.
_PLUGIN_PROVIDERS: dict[str, ProviderSpec] = {}


def set_plugin_providers(specs: dict[str, ProviderSpec]) -> None:
    """Replace the plugin-registered provider table.

    Called by `pyagent.plugins.load()` after plugins finish their
    `register()` pass. Subagent processes call this independently with
    their own (possibly narrower) plugin set, so cross-process state
    is naturally isolated.
    """
    _PLUGIN_PROVIDERS.clear()
    _PLUGIN_PROVIDERS.update(specs)


def _by_name(name: str) -> ProviderSpec | None:
    for p in PROVIDERS:
        if p.name == name:
            return p
    return _PLUGIN_PROVIDERS.get(name)


def get_client(model: str) -> "LLMClient":
    """Resolve a "provider/model" string to a concrete LLMClient instance.

    Examples:
        get_client("anthropic")                     # AnthropicClient defaults
        get_client("anthropic/claude-opus-4-7")     # explicit model
        get_client("openai/gpt-4o")
        get_client("gemini/gemini-2.5-flash")
        get_client("pyagent/echo")                  # local stub, no API call
        get_client("echo-plugin/echo")              # plugin-registered

    Raises:
        ValueError: If the provider is unknown. Built-in and
            plugin-registered names are listed in the error message.
    """
    provider, _, name = model.partition("/")
    spec = _by_name(provider)
    if spec is None:
        builtins = [p.name for p in PROVIDERS]
        plugin_names = sorted(_PLUGIN_PROVIDERS)
        known = ", ".join(builtins + plugin_names)
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


@dataclass(frozen=True)
class ProviderListing:
    """One provider's slice of the model catalog.

    `models` is the names returned by the provider's `list_models`
    callable. `error` is set instead when the callable raised — the
    CLI renders this as `(unavailable: <error>)` so a stopped
    Ollama server (or similar) doesn't kill the whole listing.
    `default_model` mirrors `ProviderSpec.default_model` so renderers
    can mark the active default with a `(default)` tag without a
    second lookup.
    """

    name: str
    default_model: str
    models: tuple[ModelInfo, ...] = ()
    error: str = ""


def list_all_models() -> list[ProviderListing]:
    """Walk every registered provider (built-in + plugin) and collect
    its advertised models.

    Built-in providers come first (in `PROVIDERS` order); plugin
    providers follow in registration order. Providers without a
    `list_models` callable get a `ProviderListing` with empty
    `models`. Providers whose callable raises get `error` populated
    so the CLI can render `(unavailable: <reason>)` per-provider —
    one bad source (e.g. ollama with the server stopped) never
    kills the rest of the listing.
    """
    out: list[ProviderListing] = []
    seen: set[str] = set()
    for spec in PROVIDERS:
        out.append(_listing_for(spec))
        seen.add(spec.name)
    for name, spec in _PLUGIN_PROVIDERS.items():
        if name in seen:
            continue
        out.append(_listing_for(spec))
    return out


def _listing_for(spec: ProviderSpec) -> ProviderListing:
    if spec.list_models is None:
        return ProviderListing(
            name=spec.name, default_model=spec.default_model
        )
    try:
        models = spec.list_models()
    except Exception as e:
        return ProviderListing(
            name=spec.name,
            default_model=spec.default_model,
            error=str(e) or e.__class__.__name__,
        )
    return ProviderListing(
        name=spec.name,
        default_model=spec.default_model,
        models=tuple(models),
    )


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

    The return value is a single assistant message in that same shape,
    even when streaming is in use — the contract is "one turn out per
    call." Streaming is purely a UX channel: incremental text chunks
    flow through `on_text_delta` while the call is in flight, and the
    final dict still carries the fully-accumulated text + tool_calls +
    usage at end-of-stream. The agent loop's tool-dispatch logic
    relies on the complete tool_calls list, so tool calls are never
    streamed piecemeal — only text is.

    `on_text_delta` is optional. Providers that haven't implemented
    streaming ignore the kwarg and behave exactly as before; the
    callback simply doesn't fire. Providers that do stream call the
    callback zero-or-more times with a chunk of plain text each
    (whatever granularity the wire format hands them — tokens,
    sentences, server-sent-event frames). Implementations must
    accumulate the same text into the returned dict's `text` field
    so a non-streaming consumer of the dict sees the full reply.
    """

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]: ...
