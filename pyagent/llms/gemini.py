"""Gemini implementation of the LLM client interface."""

import os
from typing import Any, Callable

from google import genai
from google.genai import types


# Hardcoded context windows per model. Gemini 2.5 family ships with
# 2M tokens; Gemini 2.0 has the older 1M ceiling. Default to the
# more conservative 1M for any unknown model so we don't over-promise
# on a name we haven't catalogued.
_CONTEXT_WINDOWS = {
    "gemini-2.5-flash": 2_000_000,
    "gemini-2.5-pro": 2_000_000,
    "gemini-2.0-flash": 1_000_000,
}
_DEFAULT_CONTEXT_WINDOW = 1_000_000


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
        self.provider_model = f"gemini/{model}"
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY"
        )
        if not key:
            raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        self._client = genai.Client(api_key=key)

    @property
    def context_window(self) -> int:
        """Maximum prompt-token capacity for this model. See module
        docstring for the mapping; falls back to the conservative
        1M default for unknown model names."""
        return _CONTEXT_WINDOWS.get(self.model, _DEFAULT_CONTEXT_WINDOW)

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        contents = [self._to_gemini(m) for m in conversation]
        config = self._build_config(system, tools, system_volatile)

        if on_text_delta is None:
            response = self._client.models.generate_content(
                model=self.model, contents=contents, config=config
            )
            return self._build_response_from_candidate(
                response.candidates[0].content.parts or [],
                getattr(response, "usage_metadata", None),
            )

        # Streaming: each chunk has `candidates[0].content.parts` with
        # text and/or function_call parts. Gemini emits text in chunks
        # but tool calls usually arrive complete in one chunk (not
        # incrementally), so accumulation is straightforward — text
        # parts append, function_call parts append once.
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage_meta = None
        next_call_idx = 0
        for chunk in self._client.models.generate_content_stream(
            model=self.model, contents=contents, config=config
        ):
            if chunk.candidates:
                for part in chunk.candidates[0].content.parts or []:
                    if part.text:
                        on_text_delta(part.text)
                        text_parts.append(part.text)
                    if part.function_call:
                        tool_calls.append(
                            {
                                "id": part.function_call.id
                                or f"call_{next_call_idx}",
                                "name": part.function_call.name,
                                "args": _to_plain(
                                    part.function_call.args or {}
                                ),
                            }
                        )
                        next_call_idx += 1
            if chunk.usage_metadata is not None:
                usage_meta = chunk.usage_metadata

        return self._build_response_from_parts(
            "".join(text_parts), tool_calls, usage_meta
        )

    def _build_config(
        self,
        system: str | None,
        tools: list[dict[str, Any]] | None,
        system_volatile: str | None,
    ) -> "types.GenerateContentConfig | None":
        """Build the shared `GenerateContentConfig` accepted by both
        `generate_content` and `generate_content_stream`."""
        # Gemini implicit caching keys on the prefix; concatenate
        # stable + volatile into one system_instruction. Volatile
        # mutating defeats the cache for its bytes; stable prefix
        # still benefits.
        full_system = system or ""
        if system_volatile:
            full_system = (
                f"{full_system}\n\n{system_volatile}"
                if full_system
                else system_volatile
            )
        config_kwargs: dict[str, Any] = {}
        if full_system:
            config_kwargs["system_instruction"] = full_system
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
        return (
            types.GenerateContentConfig(**config_kwargs)
            if config_kwargs
            else None
        )

    def _build_response_from_candidate(
        self, parts: list[Any], usage_meta: Any
    ) -> dict[str, Any]:
        """Translate the parts list from a non-streaming response into
        the agent-facing dict."""
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
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
        return self._build_response_from_parts(
            "".join(text_parts), tool_calls, usage_meta
        )

    def _build_response_from_parts(
        self,
        text: str,
        tool_calls: list[dict[str, Any]],
        usage_meta: Any,
    ) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": text,
            "tool_calls": tool_calls,
            "usage": {
                "input": getattr(usage_meta, "prompt_token_count", 0) or 0,
                "output": getattr(usage_meta, "candidates_token_count", 0) or 0,
                "cache_creation": 0,
                "cache_read": getattr(usage_meta, "cached_content_token_count", 0) or 0,
                "model": self.provider_model,
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
        if message.get("content"):
            parts.append(types.Part(text=message["content"]))
        for tc in message.get("tool_calls", []):
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(name=tc["name"], args=tc["args"])
                )
            )
        return types.Content(role="model", parts=parts)
