"""Smoke for the pyagent top-level library surface.

Locks the public re-exports + auto_client behavior. These are the
names the README/library-usage.md teach; if they go missing, the
documented quickstart breaks.

  1. **Top-level imports resolve.** ``from pyagent import Agent,
     Session, Attachment, LLMClient, auto_client, get_client,
     resolve_model`` works without the user having to dig into
     submodule paths.
  2. **`auto_client` raises a clear error when no API key is set.**
     The error message names the env vars the user should set.
  3. **`auto_client` picks the right provider when an env var is
     set.** Anthropic-key set → AnthropicClient.
  4. **Bare-Agent quickstart runs end-to-end against the local
     `pyagent/echo` stub** — proves the documented shape is correct
     without needing a real API key in the test runner.
  5. **`resolve_model` resolves provider shorthand to provider/model.**

Run with:
    .venv/bin/python -m tests.smoke_library_usage
"""

from __future__ import annotations

import os
from unittest import mock


def _check_top_level_imports() -> None:
    """Public surface — these names are what README/library-usage.md
    teach. Removing one without thinking would break documentation."""
    from pyagent import (
        Agent,
        Attachment,
        LLMClient,
        Session,
        auto_client,
        get_client,
        resolve_model,
    )
    # Spot-check that what's imported is what's expected.
    from pyagent.agent import Agent as DirectAgent
    from pyagent.session import Attachment as DirectAttachment, Session as DirectSession
    assert Agent is DirectAgent
    assert Attachment is DirectAttachment
    assert Session is DirectSession
    assert callable(auto_client)
    assert callable(get_client)
    assert callable(resolve_model)
    # LLMClient is a Protocol — confirm it's importable, that's enough.
    assert LLMClient is not None
    print("✓ top-level: Agent / Session / Attachment / auto_client / get_client / resolve_model")


def _check_auto_client_no_keys_clear_error() -> None:
    """Without any API key in the env, auto_client raises with a
    message that names what to set."""
    import pyagent

    keys = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    )
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        try:
            pyagent.auto_client()
        except RuntimeError as e:
            msg = str(e)
        else:
            raise AssertionError("auto_client should raise when no key is set")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert "ANTHROPIC_API_KEY" in msg, msg
    assert "OPENAI_API_KEY" in msg, msg
    assert "GEMINI_API_KEY" in msg, msg
    print("✓ auto_client: raises RuntimeError naming all known env vars")


def _check_auto_client_picks_anthropic() -> None:
    """With ANTHROPIC_API_KEY set, auto_client returns an
    AnthropicClient. We don't actually call respond() — that'd need a
    real key — just confirm the factory dispatched correctly."""
    import pyagent
    from pyagent.llms.anthropic import AnthropicClient

    # AnthropicClient.__init__ requires a key and validates eagerly.
    # Use a placeholder that's enough to construct the client; we
    # don't make any API calls.
    with mock.patch.dict(
        os.environ, {"ANTHROPIC_API_KEY": "test-placeholder"}, clear=False
    ):
        client = pyagent.auto_client()
    assert isinstance(client, AnthropicClient), type(client)
    print("✓ auto_client: ANTHROPIC_API_KEY → AnthropicClient")


def _check_bare_agent_against_echo_stub() -> None:
    """The README's quickstart shape against the pyagent/echo stub.
    No real API key needed — this proves the documented surface
    actually works."""
    import pyagent

    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    client = pyagent.get_client("pyagent/echo")
    agent = pyagent.Agent(
        client=client,
        system="You are a helpful calculator.",
    )
    agent.add_tool("add", add)
    out = agent.run("What is 17 + 25?")
    # echo stub just returns the most recent user message verbatim;
    # what we care about is that run() returns a str without raising
    # and the agent's tools dict has our function.
    assert isinstance(out, str), type(out)
    assert agent.tools.get("add") is add
    # Cumulative usage is tracked even on the stub.
    assert "input" in agent.token_usage
    assert "output" in agent.token_usage
    print("✓ bare Agent quickstart runs against pyagent/echo; tool registered")


def _check_resolve_model_shorthand() -> None:
    """resolve_model('anthropic') → 'anthropic/<default>'."""
    import pyagent

    resolved = pyagent.resolve_model("anthropic")
    assert resolved.startswith("anthropic/"), resolved
    assert resolved != "anthropic", resolved
    # Explicit form passes through unchanged.
    assert pyagent.resolve_model("openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    # Unknown provider returns unchanged so get_client can raise a
    # pointed error.
    assert pyagent.resolve_model("not-a-provider") == "not-a-provider"
    print("✓ resolve_model: shorthand fills default; explicit passes through")


def main() -> None:
    _check_top_level_imports()
    _check_auto_client_no_keys_clear_error()
    _check_auto_client_picks_anthropic()
    _check_bare_agent_against_echo_stub()
    _check_resolve_model_shorthand()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
