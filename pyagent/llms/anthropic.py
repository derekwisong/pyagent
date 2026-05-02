"""Anthropic implementation of the LLM client interface."""

import os
from typing import Any, Callable

from anthropic import Anthropic


class AnthropicClient:
    """Wraps the Anthropic SDK in our standardized client interface.

    The internal conversation format is already Anthropic-shaped, so translation
    is mostly mechanical: user/tool messages become typed content blocks,
    assistant responses are unpacked into text + tool_calls.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 16384,
        api_key: str | None = None,
        cache: bool = True,
    ) -> None:
        self.model = model
        # Full provider/model identifier persisted in each turn's usage
        # dict so the audit can price per-turn correctly without the
        # caller having to remember which model the session used.
        self.provider_model = f"anthropic/{model}"
        self.max_tokens = max_tokens
        self.cache = cache
        api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self._client = Anthropic(api_key=api_key)

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
            response = self._client.messages.create(**kwargs)
            return self._build_response(response)
        # Streaming path: `messages.stream()` is a context manager that
        # yields incremental text via `text_stream` and assembles the
        # full message (content blocks + usage) at exit. We forward
        # text chunks through the callback while the stream is in
        # flight, then read `get_final_message()` outside the context
        # so the SDK has finalised the assembly.
        with self._client.messages.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                if chunk:
                    on_text_delta(chunk)
            final = stream.get_final_message()
        return self._build_response(final)

    def _build_kwargs(
        self,
        conversation: list[dict[str, Any]],
        system: str | None,
        tools: list[dict[str, Any]] | None,
        system_volatile: str | None,
    ) -> dict[str, Any]:
        """Translate pyagent's internal conversation into the kwargs
        dict accepted by both ``messages.create`` and ``messages.stream``.

        The two SDK methods share parameter shape exactly, so the
        translation lives here once and feeds both branches of
        ``respond()``.
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self._to_anthropic(m) for m in conversation],
        }
        if system or system_volatile:
            # Two-block layout when the caller supplied BOTH a stable
            # prefix AND a volatile tail: stable block carries
            # cache_control, volatile block does not. Volatile content
            # can change turn-to-turn without invalidating the cached
            # prefix.
            #
            # Edge case: if `system` is empty/None but volatile is set,
            # we cannot emit an empty stable block — Anthropic 400s on
            # empty text content. Fall back to single-block layout
            # carrying the volatile content (no cache benefit, but
            # correct).
            stable = (system or "").strip()
            volatile = (system_volatile or "").strip()
            if self.cache and stable and volatile:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": system_volatile,
                    },
                ]
            elif self.cache and stable:
                kwargs["system"] = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
            elif self.cache and volatile:
                # Volatile-only: no cache marker, single block.
                kwargs["system"] = [
                    {"type": "text", "text": system_volatile}
                ]
            else:
                kwargs["system"] = (
                    f"{system or ''}\n\n{system_volatile}"
                    if system_volatile
                    else system
                )
        if tools:
            if self.cache:
                tools = [{**t} for t in tools]
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = tools
        return kwargs

    def _build_response(self, message: Any) -> dict[str, Any]:
        """Translate one Anthropic ``Message`` into the agent-facing
        assistant turn dict.

        Used by both branches of ``respond()`` — the streaming branch
        reads ``stream.get_final_message()`` (same Message shape as
        ``messages.create`` returns) so this helper handles both cases
        without conditionals.
        """
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in message.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "args": dict(block.input)}
                )

        usage = getattr(message, "usage", None)
        return {
            "role": "assistant",
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage, "input_tokens", 0) or 0,
                "output": getattr(usage, "output_tokens", 0) or 0,
                "cache_creation": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
                "model": self.provider_model,
            },
        }

    @staticmethod
    def _to_anthropic(message: dict[str, Any]) -> dict[str, Any]:
        if message["role"] == "user":
            if "tool_results" in message:
                return {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r["id"],
                            # Anthropic 400s on empty content blocks; the
                            # other providers accept empty strings, so the
                            # placeholder lives here, not in the agent.
                            "content": r["content"] or "<empty>",
                        }
                        for r in message["tool_results"]
                    ],
                }
            return {"role": "user", "content": message["content"]}

        # assistant
        content: list[dict[str, Any]] = []
        if message.get("text"):
            content.append({"type": "text", "text": message["text"]})
        for tc in message.get("tool_calls", []):
            content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc["args"],
                }
            )
        return {"role": "assistant", "content": content}
