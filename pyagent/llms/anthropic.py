"""Anthropic implementation of the LLM client interface."""

import os
from typing import Any

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
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [self._to_anthropic(m) for m in conversation],
        }
        if system:
            kwargs["system"] = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if self.cache
                else system
            )
        if tools:
            if self.cache:
                tools = [{**t} for t in tools]
                tools[-1]["cache_control"] = {"type": "ephemeral"}
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    {"id": block.id, "name": block.name, "args": dict(block.input)}
                )

        usage = getattr(response, "usage", None)
        return {
            "role": "assistant",
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage, "input_tokens", 0) or 0,
                "output": getattr(usage, "output_tokens", 0) or 0,
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
