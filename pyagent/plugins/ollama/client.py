"""Ollama HTTP client implementing the LLMClient protocol.

Talks to the local Ollama server's native ``/api/chat`` endpoint via
``requests`` (already a pyagent dep — keeps this provider zero-extra-
install). Uses a non-streaming POST: the ``respond()`` contract
returns one fully-formed assistant turn, and Ollama hands one back
when ``stream=False``.

Tool-call translation mirrors OpenAI's chat-completions shape, which
Ollama deliberately mimics. Two notable departures:

  - Ollama tool calls have no ``id`` field. We synthesize ``call_<i>``
    indices so downstream code in ``pyagent.agent`` (which keys tool
    results by id) keeps working.
  - Tool-result messages are routed by ``tool_name`` rather than
    ``tool_call_id``. The internal pyagent message carries both, so
    we just pass ``name`` through; Ollama models that don't honor it
    still work because the call/response order in the conversation
    is preserved.

No usage cache stats — Ollama runs locally, billing is in watts.

Tools-rejection handling: vision / embedding / base-no-template models
respond to ``/api/chat`` with a 400 ``"<model> does not support tools"``
when the request includes a ``tools`` field. The client catches that
specific error, retries once without tools, and latches the decision
on the instance so subsequent turns skip the failed round trip. Other
4xx / 5xx errors propagate.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)


DEFAULT_HOST = "http://localhost:11434"
# Long timeout because the first request after a cold start can sit
# waiting for Ollama to mmap/load a multi-GB GGUF before any tokens
# come back.
DEFAULT_TIMEOUT = 600


def _raise_with_body(resp: requests.Response, where: str) -> None:
    """Like ``resp.raise_for_status()`` but folds Ollama's JSON error
    body into the exception message.

    Ollama's ``/api/chat`` 400s on real, actionable problems — e.g.
    ``"<model> does not support tools"`` for vision/embedding models,
    or ``"model not found"`` when a tag isn't pulled — and surfaces
    the explanation in the response body's ``error`` field. The
    default ``raise_for_status`` discards that body, leaving callers
    with a bare ``400 Client Error`` they can't act on. This helper
    pulls the body out and includes it in the raised ``HTTPError`` so
    the agent's traceback shows the actual cause.
    """
    if resp.ok:
        return
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            detail = str(body.get("error") or "").strip()
        else:
            detail = str(body)
    except (ValueError, requests.exceptions.JSONDecodeError):
        # Non-JSON body — fall back to the raw text, capped so a stray
        # HTML error page doesn't blow up the log.
        detail = (resp.text or "").strip()[:500]
    msg = f"Ollama {where} returned {resp.status_code}"
    if detail:
        msg = f"{msg}: {detail}"
    raise requests.HTTPError(msg, response=resp)


def _resolve_host() -> str:
    """Resolve the Ollama server URL from env, with scheme normalised.

    Ollama itself accepts bare ``host:port`` in ``OLLAMA_HOST``; we
    upgrade to ``http://host:port`` so ``requests`` doesn't reject it.
    """
    raw = os.environ.get("OLLAMA_HOST", "").strip() or DEFAULT_HOST
    if "://" not in raw:
        raw = f"http://{raw}"
    return raw.rstrip("/")


def list_models(host: str | None = None, timeout: float = 30) -> list[dict[str, Any]]:
    """Return the parsed ``models`` list from ``GET /api/tags``.

    Raises whatever ``requests`` raises on connection/timeout failure
    so callers can surface a tagged error string.
    """
    url = f"{(host or _resolve_host()).rstrip('/')}/api/tags"
    resp = requests.get(url, timeout=timeout)
    _raise_with_body(resp, "/api/tags")
    data = resp.json()
    models = data.get("models") or []
    if not isinstance(models, list):
        return []
    return models


class OllamaClient:
    """Wraps a local Ollama HTTP server in the pyagent LLMClient interface.

    Construction does not touch the network. The first request is
    deferred to ``respond()`` so a missing or stopped server doesn't
    block agent startup, and so a session that resolves to an
    Ollama-backed model can still configure itself even if the server
    is briefly down.
    """

    def __init__(
        self,
        model: str,
        host: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not model:
            raise ValueError(
                "OllamaClient requires a model name; got empty string"
            )
        self.model = model
        self.provider_model = f"ollama/{model}"
        self.host = (host or _resolve_host()).rstrip("/")
        self.timeout = timeout
        # Latched once we discover (via a 400 retry) that this model
        # rejects the `tools` field. Subsequent turns skip tools so we
        # don't burn a wasted round trip per call. We avoid a
        # `/api/show` preflight on construction so the lazy-network
        # contract holds — the cost is exactly one extra failed
        # request the first time a no-tools model is used.
        self._skip_tools = False

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        # Ollama has no prefix-cache surface to preserve, so stable +
        # volatile concatenate into one system message — same shape
        # the OpenAI client uses.
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
            messages.extend(self._to_ollama(m))

        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools and not self._skip_tools:
            body["tools"] = [
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

        resp = requests.post(
            f"{self.host}/api/chat", json=body, timeout=self.timeout
        )
        try:
            _raise_with_body(resp, "/api/chat")
        except requests.HTTPError as e:
            # If Ollama rejected the request because the model doesn't
            # support tools, retry once without them and latch the
            # decision so subsequent turns don't repeat the failed
            # round trip. We also warn so the user knows the agent is
            # operating without its tool surface.
            if (
                "does not support tools" in str(e).lower()
                and "tools" in body
            ):
                logger.warning(
                    "ollama model %r does not support tools; retrying "
                    "without (subsequent turns in this session will skip "
                    "tools too — pyagent's agent loop relies on tools, so "
                    "consider switching to a tool-capable model)",
                    self.model,
                )
                self._skip_tools = True
                body.pop("tools")
                resp = requests.post(
                    f"{self.host}/api/chat",
                    json=body,
                    timeout=self.timeout,
                )
                _raise_with_body(resp, "/api/chat")
            else:
                raise
        data = resp.json()

        message = data.get("message") or {}
        text = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[dict[str, Any]] = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                # Some Ollama versions/models hand back JSON-stringified
                # arguments; normalise to dict so the agent sees a
                # uniform shape regardless of model quirks.
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            tool_calls.append(
                {
                    "id": tc.get("id") or f"call_{i}",
                    "name": fn.get("name") or "",
                    "args": args or {},
                }
            )

        return {
            "role": "assistant",
            "text": text,
            "tool_calls": tool_calls,
            "usage": {
                "input": int(data.get("prompt_eval_count") or 0),
                "output": int(data.get("eval_count") or 0),
                "cache_creation": 0,
                "cache_read": 0,
                "model": self.provider_model,
            },
        }

    @staticmethod
    def _to_ollama(message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["role"] == "user":
            if "tool_results" in message:
                # Ollama's tool-result wire shape is just role="tool"
                # with content; tool_name is a hint that newer models
                # honor and older ones ignore safely.
                return [
                    {
                        "role": "tool",
                        "tool_name": r.get("name", ""),
                        "content": r.get("content") or "",
                    }
                    for r in message["tool_results"]
                ]
            return [{"role": "user", "content": message["content"]}]

        # assistant — content is required even when only tool_calls
        # are present, so default to "" rather than omitting.
        msg: dict[str, Any] = {
            "role": "assistant",
            "content": message.get("text") or "",
        }
        if message.get("tool_calls"):
            msg["tool_calls"] = [
                {
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["args"],
                    },
                }
                for tc in message["tool_calls"]
            ]
        return [msg]
