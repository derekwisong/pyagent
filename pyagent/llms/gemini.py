"""Gemini implementation of the LLM client interface."""

import os
from typing import Any

from google import genai
from google.genai import types


def _to_plain(value: Any) -> Any:
    """Recursively coerce protobuf composite types (MapComposite,
    RepeatedComposite) into plain dict/list so json.dumps doesn't choke
    when the conversation gets persisted.
    """
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if hasattr(value, "items") and callable(value.items):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
        return [_to_plain(v) for v in value]
    return value


class GeminiClient:
    """Wraps the google-genai SDK in our standardized client interface.

    Translates our Anthropic-shaped internal format to Gemini's `Content` /
    `Part` shape: roles map "assistant" -> "model", text/tool_use/tool_result
    blocks become text/function_call/function_response parts. Gemini matches
    function responses by tool name, so the internal tool_results carry both
    `id` and `name`; we use `name` here.
    """

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
    ) -> None:
        self.model = model
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY"
        )
        if not key:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        self._client = genai.Client(api_key=key)

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contents = [self._to_gemini(m) for m in conversation]

        config_kwargs: dict[str, Any] = {}
        if system:
            config_kwargs["system_instruction"] = system
        if tools:
            config_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t["name"],
                            description=t["description"],
                            parameters=t["input_schema"],
                        )
                        for t in tools
                    ]
                )
            ]
        config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

        response = self._client.models.generate_content(
            model=self.model, contents=contents, config=config
        )

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        parts = response.candidates[0].content.parts or []
        for i, part in enumerate(parts):
            if part.text:
                text_parts.append(part.text)
            if part.function_call:
                tool_calls.append(
                    {
                        "id": part.function_call.id or f"call_{i}",
                        "name": part.function_call.name,
                        "args": _to_plain(part.function_call.args or {}),
                    }
                )

        usage_meta = getattr(response, "usage_metadata", None)
        return {
            "role": "assistant",
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage_meta, "prompt_token_count", 0) or 0,
                "output": getattr(usage_meta, "candidates_token_count", 0) or 0,
            },
        }

    @staticmethod
    def _to_gemini(message: dict[str, Any]) -> types.Content:
        if message["role"] == "user":
            if "tool_results" in message:
                return types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                name=r["name"],
                                response={"result": r["content"]},
                            )
                        )
                        for r in message["tool_results"]
                    ],
                )
            # Gemini rejects empty Part(text=""); coerce to a single
            # space so the conversation shape stays valid.
            return types.Content(
                role="user",
                parts=[types.Part(text=message["content"] or " ")],
            )

        # assistant -> "model"
        parts: list[types.Part] = []
        if message.get("text"):
            parts.append(types.Part(text=message["text"]))
        for tc in message.get("tool_calls", []):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(name=tc["name"], args=tc["args"])
                )
            )
        return types.Content(role="model", parts=parts)
