"""OpenAI implementation of the LLM client interface."""

import json
import os
from typing import Any

from openai import OpenAI


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
        max_tokens: int = 4096,
        api_key: str | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = OpenAI(api_key=api_key or os.environ.get("OPENAI_API_KEY"))

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
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

        response = self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        tool_calls: list[dict[str, Any]] = []
        for tc in message.tool_calls or []:
            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "args": json.loads(tc.function.arguments or "{}"),
                }
            )

        usage = getattr(response, "usage", None)
        return {
            "role": "assistant",
            "text": message.content or "",
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage, "prompt_tokens", 0) or 0,
                "output": getattr(usage, "completion_tokens", 0) or 0,
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
        if message.get("text"):
            msg["content"] = message["text"]
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
