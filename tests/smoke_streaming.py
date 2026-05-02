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

import json
import os
from unittest import mock

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


# ---- Per-provider streaming (mocked SDKs) -----------------------
#
# Each provider is exercised via mock so the test runs without API
# keys and without making any network calls. The mocks shape-match
# what each SDK actually returns so the client's translation logic
# is genuinely exercised, not stubbed away.


class _Attr:
    """Tiny record-style object — exposes whatever kwargs you hand it
    as attributes. Used to build SDK-shaped mocks without dragging in
    pydantic models."""

    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _check_anthropic_streaming_mocked() -> None:
    """AnthropicClient.respond with on_text_delta uses messages.stream
    and assembles final dict from get_final_message()."""

    os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-smoke")
    from pyagent.llms.anthropic import AnthropicClient

    final_msg = _Attr(
        content=[
            _Attr(type="text", text="Hello world"),
            _Attr(type="tool_use", id="t1", name="lookup", input={"q": "x"}),
        ],
        usage=_Attr(
            input_tokens=5,
            output_tokens=12,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )

    class _FakeStreamCM:
        def __init__(self, chunks, final):
            self._chunks = chunks
            self._final = final
            self.text_stream = iter(chunks)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return self._final

    captured: dict = {}

    def fake_stream(**kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return _FakeStreamCM(["Hello ", "world"], final_msg)

    client = AnthropicClient(model="claude-sonnet-4-6", api_key="dummy")
    deltas: list[str] = []
    with mock.patch.object(client._client.messages, "stream", side_effect=fake_stream):
        out = client.respond(
            conversation=[{"role": "user", "content": "hi"}],
            on_text_delta=deltas.append,
        )

    _check("anthropic stream() invoked", captured.get("called") is True)
    _check(
        "anthropic deltas arrive in chunk order",
        deltas == ["Hello ", "world"],
        repr(deltas),
    )
    _check(
        "anthropic concatenated text matches returned",
        "".join(deltas) == out["text"] == "Hello world",
        repr(out["text"]),
    )
    _check(
        "anthropic tool_use captured into tool_calls",
        len(out["tool_calls"]) == 1
        and out["tool_calls"][0] == {"id": "t1", "name": "lookup", "args": {"q": "x"}},
        repr(out["tool_calls"]),
    )
    _check(
        "anthropic usage parsed",
        out["usage"]["input"] == 5 and out["usage"]["output"] == 12,
        repr(out["usage"]),
    )

    # No callback → uses messages.create, NOT messages.stream.
    create_calls: list = []

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return final_msg

    stream_calls: list = []

    def fake_stream_should_not_run(**kwargs):
        stream_calls.append(kwargs)
        return _FakeStreamCM([], final_msg)

    fresh = AnthropicClient(model="claude-sonnet-4-6", api_key="dummy")
    with mock.patch.object(fresh._client.messages, "create", side_effect=fake_create):
        with mock.patch.object(
            fresh._client.messages, "stream", side_effect=fake_stream_should_not_run
        ):
            out2 = fresh.respond(conversation=[{"role": "user", "content": "x"}])
    _check(
        "anthropic no-callback uses messages.create",
        len(create_calls) == 1 and len(stream_calls) == 0,
        f"create={len(create_calls)} stream={len(stream_calls)}",
    )
    _check(
        "anthropic no-callback returns same dict shape",
        out2["text"] == "Hello world" and len(out2["tool_calls"]) == 1,
        repr(out2),
    )

def _check_openai_streaming_mocked() -> None:
    """OpenAIClient.respond with on_text_delta uses
    chat.completions.create(stream=True) and accumulates tool-call
    arguments by index across chunks."""

    os.environ.setdefault("OPENAI_API_KEY", "dummy-for-smoke")
    from pyagent.llms.openai import OpenAIClient

    # Stream of chunks: text deltas, then tool-call argument
    # fragments (OpenAI splits the JSON args string across chunks),
    # then a final usage chunk.
    chunks = [
        _Attr(
            choices=[_Attr(delta=_Attr(content="Looking ", tool_calls=None))],
            usage=None,
        ),
        _Attr(
            choices=[_Attr(delta=_Attr(content="this up...", tool_calls=None))],
            usage=None,
        ),
        _Attr(
            choices=[
                _Attr(
                    delta=_Attr(
                        content=None,
                        tool_calls=[
                            _Attr(
                                index=0,
                                id="call_42",
                                function=_Attr(name="lookup", arguments='{"q":'),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        _Attr(
            choices=[
                _Attr(
                    delta=_Attr(
                        content=None,
                        tool_calls=[
                            _Attr(
                                index=0,
                                id=None,
                                function=_Attr(name=None, arguments=' "x"}'),
                            )
                        ],
                    )
                )
            ],
            usage=None,
        ),
        _Attr(
            choices=[],
            usage=_Attr(
                prompt_tokens=8,
                completion_tokens=15,
                prompt_tokens_details=_Attr(cached_tokens=2),
            ),
        ),
    ]

    captured: dict = {}

    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        return iter(chunks)

    client = OpenAIClient(model="gpt-4o", api_key="dummy")
    deltas: list[str] = []
    with mock.patch.object(
        client._client.chat.completions, "create", side_effect=fake_create
    ):
        out = client.respond(
            conversation=[{"role": "user", "content": "hi"}],
            on_text_delta=deltas.append,
        )

    _check(
        "openai create called with stream=True",
        captured["kwargs"].get("stream") is True,
        repr(captured["kwargs"].get("stream")),
    )
    _check(
        "openai create called with include_usage option",
        captured["kwargs"].get("stream_options") == {"include_usage": True},
        repr(captured["kwargs"].get("stream_options")),
    )
    _check(
        "openai deltas arrive in chunk order",
        deltas == ["Looking ", "this up..."],
        repr(deltas),
    )
    _check(
        "openai concat deltas == returned text",
        "".join(deltas) == out["text"] == "Looking this up...",
        repr(out["text"]),
    )
    _check(
        "openai accumulates tool_call args across chunks",
        len(out["tool_calls"]) == 1
        and out["tool_calls"][0]["id"] == "call_42"
        and out["tool_calls"][0]["name"] == "lookup"
        and out["tool_calls"][0]["args"] == {"q": "x"},
        repr(out["tool_calls"]),
    )
    _check(
        "openai usage parsed including cache_read",
        out["usage"]["input"] == 8
        and out["usage"]["output"] == 15
        and out["usage"]["cache_read"] == 2,
        repr(out["usage"]),
    )

    # No callback → no stream kwarg, returns from a non-iterator response.
    non_stream_response = _Attr(
        choices=[
            _Attr(
                message=_Attr(
                    content="non-stream",
                    tool_calls=None,
                )
            )
        ],
        usage=_Attr(
            prompt_tokens=1,
            completion_tokens=2,
            prompt_tokens_details=None,
        ),
    )
    captured.clear()

    def fake_create_one_shot(**kwargs):
        captured["kwargs"] = kwargs
        return non_stream_response

    fresh = OpenAIClient(model="gpt-4o", api_key="dummy")
    with mock.patch.object(
        fresh._client.chat.completions, "create", side_effect=fake_create_one_shot
    ):
        out2 = fresh.respond(conversation=[{"role": "user", "content": "x"}])
    _check(
        "openai no-callback omits stream from kwargs",
        "stream" not in captured["kwargs"],
        repr(captured["kwargs"]),
    )
    _check(
        "openai no-callback returns full text",
        out2["text"] == "non-stream",
        repr(out2),
    )


def _check_gemini_streaming_mocked() -> None:
    """GeminiClient.respond with on_text_delta uses
    generate_content_stream and folds usage from the final chunk."""

    os.environ.setdefault("GEMINI_API_KEY", "dummy-for-smoke")
    from pyagent.llms.gemini import GeminiClient

    # Chunks: text deltas then a tool-call chunk then a final usage chunk.
    # Each chunk has candidates[0].content.parts.
    chunks = [
        _Attr(
            candidates=[
                _Attr(content=_Attr(parts=[_Attr(text="Hel", function_call=None)]))
            ],
            usage_metadata=None,
        ),
        _Attr(
            candidates=[
                _Attr(content=_Attr(parts=[_Attr(text="lo", function_call=None)]))
            ],
            usage_metadata=None,
        ),
        _Attr(
            candidates=[
                _Attr(
                    content=_Attr(
                        parts=[
                            _Attr(
                                text=None,
                                function_call=_Attr(
                                    id=None, name="lookup", args={"q": "x"}
                                ),
                            )
                        ]
                    )
                )
            ],
            usage_metadata=None,
        ),
        _Attr(
            candidates=[],
            usage_metadata=_Attr(
                prompt_token_count=7,
                candidates_token_count=11,
                cached_content_token_count=3,
            ),
        ),
    ]

    captured: dict = {}

    def fake_stream(**kwargs):
        captured["called"] = True
        captured["kwargs"] = kwargs
        return iter(chunks)

    client = GeminiClient(model="gemini-2.5-flash", api_key="dummy")
    deltas: list[str] = []
    with mock.patch.object(
        client._client.models, "generate_content_stream", side_effect=fake_stream
    ):
        out = client.respond(
            conversation=[{"role": "user", "content": "hi"}],
            on_text_delta=deltas.append,
        )

    _check("gemini stream invoked", captured.get("called") is True)
    _check(
        "gemini deltas arrive in chunk order",
        deltas == ["Hel", "lo"],
        repr(deltas),
    )
    _check(
        "gemini concat deltas == returned text",
        "".join(deltas) == out["text"] == "Hello",
        repr(out["text"]),
    )
    _check(
        "gemini function_call captured",
        len(out["tool_calls"]) == 1
        and out["tool_calls"][0]["name"] == "lookup"
        and out["tool_calls"][0]["args"] == {"q": "x"}
        and out["tool_calls"][0]["id"].startswith("call_"),
        repr(out["tool_calls"]),
    )
    _check(
        "gemini usage parsed including cache_read",
        out["usage"]["input"] == 7
        and out["usage"]["output"] == 11
        and out["usage"]["cache_read"] == 3,
        repr(out["usage"]),
    )

    # No callback → non-streaming generate_content path.
    one_shot = _Attr(
        candidates=[
            _Attr(
                content=_Attr(
                    parts=[_Attr(text="non-stream", function_call=None)]
                )
            )
        ],
        usage_metadata=_Attr(
            prompt_token_count=1,
            candidates_token_count=2,
            cached_content_token_count=0,
        ),
    )
    stream_calls: list = []

    def fake_stream_should_not_run(**kwargs):
        stream_calls.append(kwargs)
        return iter([])

    fresh = GeminiClient(model="gemini-2.5-flash", api_key="dummy")
    with mock.patch.object(
        fresh._client.models, "generate_content", return_value=one_shot
    ):
        with mock.patch.object(
            fresh._client.models,
            "generate_content_stream",
            side_effect=fake_stream_should_not_run,
        ):
            out2 = fresh.respond(conversation=[{"role": "user", "content": "x"}])
    _check(
        "gemini no-callback uses generate_content (not stream)",
        len(stream_calls) == 0,
        f"stream_calls={len(stream_calls)}",
    )
    _check(
        "gemini no-callback returns full text",
        out2["text"] == "non-stream",
        repr(out2),
    )


def main() -> None:
    _check_echo_streams_word_by_word()
    _check_echo_no_callback_unchanged()
    _check_lorem_streams_sentences()
    _check_provider_signatures_accept_kwarg()
    _check_agent_forwards_delta_callback()
    _check_anthropic_streaming_mocked()
    _check_openai_streaming_mocked()
    _check_gemini_streaming_mocked()
    print("smoke_streaming: all checks passed")


if __name__ == "__main__":
    main()
