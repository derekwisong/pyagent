"""End-to-end smoke for the streaming hook on the LLMClient protocol.

Concerns:

  1. **Protocol surface.** Every existing provider client accepts the
     new ``on_text_delta`` kwarg without raising. Non-streaming
     providers (anthropic / openai / gemini stubs in this PR) ignore
     it; streaming providers (pyagent stubs, ollama) fire it.
  2. **EchoClient streams word-by-word.** With a callback set, deltas
     arrive in order and concatenate to the final ``text`` field. The
     ``text`` is preserved on the returned dict so non-streaming
     consumers still see the full reply.
  3. **LoremClient streams sentence-by-sentence.** Same concatenation
     invariant — `"".join(deltas) == returned["text"]`. Works without
     mocking randomness because we just check the deltas
     reconstruct the dict's text.
  4. **Agent loop forwards deltas.** ``Agent.run(..., on_text_delta=cb)``
     piping reaches the client's ``respond`` and the callback fires
     while the turn is still in flight. The trailing ``on_text``
     callback receives the same total text that the deltas summed to,
     so a CLI consuming both sees consistent state.
  5. **No-callback path is unchanged.** Calling ``respond()`` without
     the kwarg returns the same shape as before — providers that
     stream when set fall back to non-streaming behavior when unset.

Run with:

    .venv/bin/python -m tests.smoke_streaming
"""

from __future__ import annotations

from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient, LoremClient


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_echo_streams_word_by_word() -> None:
    deltas: list[str] = []
    out = EchoClient().respond(
        conversation=[{"role": "user", "content": "hello there friend"}],
        on_text_delta=deltas.append,
    )
    _check("got at least one delta", len(deltas) > 0, repr(deltas))
    _check(
        "deltas concatenate to returned text",
        "".join(deltas) == out["text"],
        f"deltas={deltas!r} text={out['text']!r}",
    )
    _check(
        "echo full text preserved",
        out["text"] == "hello there friend",
        out["text"],
    )


def _check_echo_no_callback_unchanged() -> None:
    """Calling respond() without on_text_delta returns the same dict
    shape as before — proves the kwarg is purely additive."""
    out = EchoClient().respond(
        conversation=[{"role": "user", "content": "hi"}],
    )
    _check(
        "no-callback echo returns full text",
        out["text"] == "hi" and out["tool_calls"] == [],
        repr(out),
    )


def _check_lorem_streams_sentences() -> None:
    deltas: list[str] = []
    out = LoremClient().respond(
        conversation=[{"role": "user", "content": "go"}],
        on_text_delta=deltas.append,
    )
    _check("lorem produced text", bool(out["text"]))
    _check(
        "lorem deltas concatenate to returned text",
        "".join(deltas) == out["text"],
        f"text len={len(out['text'])} sum-of-deltas={len(''.join(deltas))}",
    )
    _check(
        "lorem fired multiple deltas",
        len(deltas) >= 2,
        f"deltas={len(deltas)}",
    )


def _check_provider_signatures_accept_kwarg() -> None:
    """Every built-in client must accept on_text_delta — even ones
    that don't yet stream — so the agent loop can pass it
    uniformly. Constructors that need API keys are skipped (env not
    guaranteed); we only inspect the signature."""
    import inspect

    from pyagent.llms.anthropic import AnthropicClient
    from pyagent.llms.gemini import GeminiClient
    from pyagent.llms.openai import OpenAIClient
    from pyagent.plugins.ollama.client import OllamaClient

    for cls in (AnthropicClient, OpenAIClient, GeminiClient, OllamaClient):
        sig = inspect.signature(cls.respond)
        _check(
            f"{cls.__name__}.respond accepts on_text_delta",
            "on_text_delta" in sig.parameters,
            str(sig),
        )


def _check_agent_forwards_delta_callback() -> None:
    """Agent.run(on_text_delta=...) plumbing reaches the client.

    Uses EchoClient as the model so the test runs without a live
    Ollama server, doesn't burn API keys, and is deterministic.
    """
    deltas: list[str] = []
    full_text: list[str] = []

    agent = Agent(client=EchoClient(), system="x")
    final = agent.run(
        prompt="streaming please",
        on_text=full_text.append,
        on_text_delta=deltas.append,
    )

    _check("agent produced final_text", bool(final), final)
    _check(
        "agent.on_text fired with the full reply",
        full_text == ["streaming please"],
        repr(full_text),
    )
    _check(
        "agent.on_text_delta fired with concatenable chunks",
        "".join(deltas) == "streaming please",
        repr(deltas),
    )
    _check(
        "agent fired more than one delta (proves stream wasn't a single dump)",
        len(deltas) > 1,
        f"deltas={len(deltas)}",
    )


def main() -> None:
    _check_echo_streams_word_by_word()
    _check_echo_no_callback_unchanged()
    _check_lorem_streams_sentences()
    _check_provider_signatures_accept_kwarg()
    _check_agent_forwards_delta_callback()
    print("smoke_streaming: all checks passed")


if __name__ == "__main__":
    main()
