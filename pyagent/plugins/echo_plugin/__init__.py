"""echo-plugin — test/demo bundled plugin: registers an echo LLM provider.

Exercises the plugin provider surface (`api.register_provider`). When
loaded, `--model echo-plugin/<anything>` resolves through the plugin
router and produces a one-shot reply that echoes the most recent user
message — same shape as the built-in `pyagent/echo` stub, but routed
through the plugin path. Useful as:

  - A smoke test for the loader → llm-router wiring (the existence of
    `echo-plugin/echo` proves `set_plugin_providers` ran).
  - A scaffolding example for real plugin providers (cli/claude in #57,
    local-model adapters, etc.) — implementations can copy this layout
    verbatim and replace the body of `respond`.

The client is intentionally minimal; for anything more sophisticated
the right home is a separate plugin that imports its own SDK inside
the factory so unused providers don't pay the import cost.
"""

from __future__ import annotations

from typing import Any


class _PluginEchoClient:
    """Echo client routed through the plugin provider path.

    Mirrors `pyagent.llms.pyagent.EchoClient` but reports its model
    string as `echo-plugin/<model>` so cost/usage breakdowns and the
    session header surface the plugin origin clearly. The agent loop
    terminates after one turn (no tool calls), so each call is a
    self-contained reply.
    """

    def __init__(self, model: str = "echo") -> None:
        self.model = model
        self.provider_model = f"echo-plugin/{model}"

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
    ) -> dict[str, Any]:
        text = ""
        for msg in reversed(conversation):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
                break
        return {
            "content": text,
            "tool_calls": [],
            "usage": {
                "input": 0,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "model": self.provider_model,
            },
        }


def _factory(**kw: Any) -> _PluginEchoClient:
    model = kw.get("model") or "echo"
    return _PluginEchoClient(model=model)


def register(api):
    api.register_provider(
        "echo-plugin",
        _factory,
        default_model="echo",
    )
