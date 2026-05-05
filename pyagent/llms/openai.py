"""OpenAI implementation of the LLM client interface."""

import json
import os
from typing import Any, Callable

from openai import OpenAI


# Hardcoded context windows per model. OpenAI's lineup splits between
# 128K-context flagship chat models and 200K-context o-series reasoning
# models, so the table is real-data, not a uniform default. Default
# of 128_000 catches gpt-4-turbo / gpt-4o variants that ship with that
# size; o-series get an explicit override.
_CONTEXT_WINDOWS = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
}
_DEFAULT_CONTEXT_WINDOW = 128_000


class OpenAIClient:
    """Wraps the OpenAI SDK in our standardized client interface.

    Uses the chat.completions API. Translates between our Anthropic-shaped
    internal format and OpenAI's role-based message format: the system prompt
    becomes a leading role="system" message; tool calls live on assistant
    messages with JSON-stringified arguments; tool results become role="tool"
    messages keyed by tool_call_id.

    Prompt caching is automatic on the OpenAI side for inputs over ~1024
    tokens; nothing to set here.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        max_tokens: int = 16384,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.provider_model = f"openai/{model}"
        self.max_tokens = max_tokens
        self._client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    @property
    def context_window(self) -> int:
        """Maximum prompt-token capacity for this model. See module
        docstring for the mapping; falls back to the chat-model
        default for unknown names."""
        return _CONTEXT_WINDOWS.get(self.model, _DEFAULT_CONTEXT_WINDOW)

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        kwargs = self._build_kwargs(conversation, system, tools, system_volatile)
        if on_text_delta is None:
            response = self._client.chat.completions.create(**kwargs)
            return self._build_response_from_message(
                response.choices[0].message, getattr(response, "usage", None)
            )
        # Streaming: include_usage so the trailing chunk carries the
        # token counters; OpenAI omits them otherwise. Tool calls
        # arrive incrementally and are keyed by `index` — the model
        # interleaves arg-string fragments across many chunks for the
        # same call, so we accumulate per-index and join at the end.
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        text_parts: list[str] = []
        tc_acc: dict[int, dict[str, Any]] = {}
        usage = None
        for chunk in self._client.chat.completions.create(**kwargs):
            choices = getattr(chunk, "choices", None) or []
            if choices:
                delta = choices[0].delta
                if getattr(delta, "content", None):
                    on_text_delta(delta.content)
                    text_parts.append(delta.content)
                for tc_delta in getattr(delta, "tool_calls", None) or []:
                    idx = tc_delta.index
                    slot = tc_acc.setdefault(
                        idx, {"id": "", "name": "", "args_str": ""}
                    )
                    if getattr(tc_delta, "id", None):
                        slot["id"] = tc_delta.id
                    fn = getattr(tc_delta, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args_str"] += fn.arguments
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage

        tool_calls: list[dict[str, Any]] = []
        for idx in sorted(tc_acc):
            slot = tc_acc[idx]
            try:
                args = json.loads(slot["args_str"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": slot["args_str"]}
            tool_calls.append(
                {"id": slot["id"], "name": slot["name"], "args": args}
            )
        return self._build_response_from_parts(
            "".join(text_parts), tool_calls, usage
        )

    def _build_kwargs(
        self,
        conversation: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]] | None,
        system_volatile: str | None,
    ) -> dict[str, Any]:
        """Translate pyagent's internal conversation into the kwargs
        dict accepted by both the streaming and non-streaming branches
        of ``chat.completions.create``."""
        messages: list[dict[str, Any]] = []
        # OpenAI prompt caching is automatic on the prefix; concatenate
        # stable + volatile into one system message. Volatile content
        # mutating each turn defeats the cache for the volatile bytes
        # but the stable prefix still benefits.
        full_system = system or ""
        if system_volatile:
            full_system = (
                f"{full_system}\n\n{system_volatile}"
                if full_system
                else system_volatile
            )
        if full_system:
            messages.append({"role": "system", "content": full_system})
        for m in conversation:
            messages.extend(self._to_openai(m))

        kwargs: dict[str, Any] = {
            "model": self.model,
            # `max_completion_tokens` is the chat-completions name that
            # works for every model — including o-series reasoning models
            # that reject the legacy `max_tokens`.
            "max_completion_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        return kwargs

    def _build_response_from_message(
        self, message: Any, usage: Any
    ) -> dict[str, Any]:
        """Translate one non-streaming ``ChatCompletionMessage`` into
        the agent-facing assistant turn dict."""
        tool_calls: list[dict[str, Any]] = []
        for tc in message.tool_calls or []:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments or "{}"),
                }
            )
        return self._build_response_from_parts(
            message.content or "", tool_calls, usage
        )

    def _build_response_from_parts(
        self,
        text: str,
        tool_calls: list[dict[str, Any]],
        usage: Any,
    ) -> dict[str, Any]:
        """Common shape-builder. Both branches converge here so the
        returned dict's structure is identical regardless of mode."""
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cache_read = getattr(prompt_details, "cached_tokens", 0) or 0
        return {
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage, "prompt_tokens", 0) or 0,
                "output": getattr(usage, "completion_tokens", 0) or 0,
                "cache_creation": 0,
                "cache_read": cache_read,
                "model": self.provider_model,
            },
        }

    @staticmethod
    def _to_openai(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["role"] == "user":
            if "tool_results" in message:
                return [
                    {
                        "role": "tool",
                        "tool_call_id": r["id"],
                        "content": r["content"],
                    }
                    for r in message["tool_results"]
                ]
            return [{"role": "user", "content": message["content"]}]

        msg: dict[str, Any] = {"role": "assistant"}
        if message.get("content"):
            msg["content"] = message["content"]
        if message.get("tool_calls"):
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"]),
                    },
                }
                for tc in message["tool_calls"]
            ]
        return [msg]
