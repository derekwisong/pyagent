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
from collections.abc import Callable

import requests

from pyagent import config as _config
from pyagent.plugins.ollama import dialects

logger = logging.getLogger(__name__)


DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 600
# Ollama server defaults num_ctx to 2048/4096 regardless of model arch; override.
NUM_CTX_FLOOR = 8192
NUM_CTX_CAP = 16384
DEFAULT_TEMPERATURE = 0.3


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


def show_model(
    name: str, host: str | None = None, timeout: float = 30
) -> dict[str, Any]:
    """Return the parsed ``POST /api/show`` payload for one model.

    Used to extract capability tags (``"tools"``, ``"vision"``,
    ``"embedding"``, ...) — Ollama 0.5+ surfaces these in the
    ``capabilities`` array. Older servers return this field empty,
    so callers must treat the absence of capabilities as
    "unknown", not "none".
    """
    url = f"{(host or _resolve_host()).rstrip('/')}/api/show"
    resp = requests.post(url, json={"name": name}, timeout=timeout)
    _raise_with_body(resp, "/api/show")
    return resp.json()


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
            raise ValueError("OllamaClient requires a model name; got empty string")
        self.model = model
        self.provider_model = f"ollama/{model}"
        self.host = (host or _resolve_host()).rstrip("/")
        self.timeout = timeout
        self._skip_tools = False
        self._show_payload: dict[str, Any] | None = None
        self._context_window: int | None = None
        self._dialect: dialects.Dialect | None = None

    def _show(self) -> dict[str, Any]:
        """Lazy-cached ``/api/show`` payload, with empty-dict fallback
        on any failure.

        Both :attr:`context_window` and :attr:`dialect` need this
        payload, and ``/api/show`` is a real network call — so we
        consolidate. Failure latches to ``{}`` so a transient blip
        doesn't get retried forever, matching what the prior
        ``_context_window = 0`` behavior did.
        """
        if self._show_payload is None:
            try:
                self._show_payload = show_model(self.model, host=self.host)
            except Exception:
                self._show_payload = {}
        return self._show_payload

    @property
    def context_window(self) -> int:
        """Maximum prompt-token capacity for this model, looked up
        live from the server's `/api/show` payload.

        Cached after first read so the agent loop's per-turn check
        doesn't redo the HTTP call. Returns 0 when the lookup fails
        (server unreachable / older Ollama without `model_info`) —
        the CLI's context-warning machinery treats that as "window
        unknown" and hides the footer segment, matching the built-in
        stubs.
        """
        if self._context_window is not None:
            return self._context_window
        model_info = self._show().get("model_info") or {}
        if isinstance(model_info, dict):
            for key, val in model_info.items():
                if key.endswith(".context_length") and isinstance(val, int):
                    self._context_window = val
                    return val
        self._context_window = 0
        return 0

    @property
    def dialect(self) -> dialects.Dialect:
        """Per-family wire-format dialect, sniffed from the model's
        template via ``/api/show`` and cached for the session.

        Only consulted when we have to inline a tool call into the
        assistant content channel ourselves (mixed turns — see
        :meth:`_to_ollama`). Tool-call-only turns ride the structured
        ``tool_calls`` field and Ollama's own template renders the
        right envelope per model, so the dialect doesn't matter
        there.

        Falls back to the default dialect when ``/api/show`` failed
        or the template is unrecognized — see
        :func:`dialects.detect_from_template` for the precedence.
        """
        if self._dialect is None:
            template = self._show().get("template") or ""
            self._dialect = dialects.detect_from_template(template)
        return self._dialect

    def _resolve_temperature(self) -> float:
        """Pick the ``temperature`` to send with each chat request.

        Resolution order, first hit wins:

        1. ``PYAGENT_OLLAMA_TEMPERATURE`` env var — kill-switch /
           one-off override; any non-negative float.
        2. ``[ollama.temperature_per_model]<model>`` in config —
           per-model override keyed on the same string passed via
           ``--model ollama/<model>``.
        3. ``[ollama] temperature`` in config — section-wide default.
        4. :data:`DEFAULT_TEMPERATURE` — built-in fallback.

        Each step validates: non-numeric or negative values warn
        and fall through to the next tier rather than failing the
        turn. The result is fetched per-call (no caching) so editing
        config.toml mid-session takes effect on the next turn.
        """
        env = os.environ.get("PYAGENT_OLLAMA_TEMPERATURE", "").strip()
        if env:
            try:
                t = float(env)
                if t >= 0:
                    return t
                logger.warning(
                    "PYAGENT_OLLAMA_TEMPERATURE=%r is negative; ignoring",
                    env,
                )
            except ValueError:
                logger.warning(
                    "PYAGENT_OLLAMA_TEMPERATURE=%r is not a float; ignoring",
                    env,
                )

        cfg = _config.load().get("ollama") or {}
        per_model = cfg.get("temperature_per_model") or {}
        if isinstance(per_model, dict) and self.model in per_model:
            candidate = per_model[self.model]
            if (
                isinstance(candidate, (int, float))
                and not isinstance(candidate, bool)
                and candidate >= 0
            ):
                return float(candidate)
            logger.warning(
                "[ollama.temperature_per_model] %r = %r is not a "
                "non-negative number; ignoring",
                self.model,
                candidate,
            )

        candidate = cfg.get("temperature")
        if (
            isinstance(candidate, (int, float))
            and not isinstance(candidate, bool)
            and candidate >= 0
        ):
            return float(candidate)
        if candidate is not None:
            logger.warning(
                "[ollama] temperature = %r is not a non-negative " "number; ignoring",
                candidate,
            )

        return DEFAULT_TEMPERATURE

    def _resolve_num_ctx(self) -> int:
        """Pick the ``num_ctx`` value to send with each chat request.

        Priority: ``PYAGENT_OLLAMA_NUM_CTX`` env var (raw passthrough,
        for users who know what their hardware can hold) → the model's
        architectural context length capped at ``NUM_CTX_CAP`` → a
        ``NUM_CTX_FLOOR`` fallback when the lookup fails. Anything is
        better than letting Ollama default to 4096 silently — that
        dwarfs the user message under tool-doc system prompts and
        produces nonsense replies.
        """
        env = os.environ.get("PYAGENT_OLLAMA_NUM_CTX", "").strip()
        if env:
            try:
                n = int(env)
                if n > 0:
                    return n
            except ValueError:
                logger.warning(
                    "PYAGENT_OLLAMA_NUM_CTX=%r is not a positive int; ignoring",
                    env,
                )
        window = self.context_window
        if window > 0:
            return min(window, NUM_CTX_CAP)
        return NUM_CTX_FLOOR

    @property
    def effective_context_window(self) -> int:
        """The context window pyagent actually delivers to Ollama.

        ``context_window`` reports the model's architectural maximum
        (e.g. 256K for qwen3-30b-a3b). But what we *send* is
        ``_resolve_num_ctx()``, which caps at ``NUM_CTX_CAP`` (or
        whatever the user pinned via ``PYAGENT_OLLAMA_NUM_CTX``).
        Footer percentages need to divide by this — the effective
        ceiling — not the architectural one, otherwise "ctx: 5%"
        means "you're 80% of the way to truncation but the UI is
        lying."
        """
        return self._resolve_num_ctx()

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
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

        streaming = on_text_delta is not None
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": streaming,
            "options": {
                "num_ctx": self._resolve_num_ctx(),
                "temperature": self._resolve_temperature(),
            },
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

        resp = self._post_chat(body)
        if not streaming:
            return self._build_response(resp.json())
        return self._consume_stream(resp, on_text_delta)

    def _post_chat(self, body: dict[str, Any]) -> requests.Response:
        """POST /api/chat, with the no-tools-auto-retry path applied.

        Honors ``body["stream"]`` to decide whether the underlying
        ``requests`` call streams the response body — without this
        flag, a streaming chat would buffer fully before
        ``iter_lines`` even runs, defeating the point.

        On a 400 ``"does not support tools"``, retries once without
        ``tools`` and latches ``_skip_tools`` so subsequent turns
        skip the failed round trip. Other 4xx / 5xx propagate. The
        first response object is closed before the retry so a
        partially-buffered streaming connection doesn't linger.
        """
        streaming = bool(body.get("stream"))
        resp = requests.post(
            f"{self.host}/api/chat",
            json=body,
            timeout=self.timeout,
            stream=streaming,
        )
        try:
            _raise_with_body(resp, "/api/chat")
        except requests.HTTPError as e:
            if "does not support tools" in str(e).lower() and "tools" in body:
                logger.warning(
                    "ollama model %r does not support tools; retrying "
                    "without (subsequent turns in this session will skip "
                    "tools too — pyagent's agent loop relies on tools, so "
                    "consider switching to a tool-capable model)",
                    self.model,
                )
                self._skip_tools = True
                body.pop("tools")
                try:
                    resp.close()
                except Exception:
                    pass
                resp = requests.post(
                    f"{self.host}/api/chat",
                    json=body,
                    timeout=self.timeout,
                    stream=streaming,
                )
                _raise_with_body(resp, "/api/chat")
            else:
                raise
        return resp

    def _build_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """Translate one parsed `/api/chat` payload into the agent-
        facing assistant turn dict.

        Used by the non-streaming path directly and by the streaming
        path after it has assembled `data` from accumulated chunks
        — keeps tool-call id synthesis and arg normalisation in one
        place so behavior is identical between modes.
        """
        message = data.get("message") or {}
        text = message.get("content") or ""
        raw_calls = message.get("tool_calls") or []
        tool_calls: list[dict[str, Any]] = []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
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
            "content": text,
            "tool_calls": tool_calls,
            "usage": {
                "input": int(data.get("prompt_eval_count") or 0),
                "output": int(data.get("eval_count") or 0),
                "cache_creation": 0,
                "cache_read": 0,
                "model": self.provider_model,
            },
        }

    def _consume_stream(
        self,
        resp: requests.Response,
        on_text_delta: Callable[[str], None],
    ) -> dict[str, Any]:
        """Drain Ollama's NDJSON stream, firing text deltas as they
        arrive, and assemble the final agent-facing turn dict.

        Wire format: each line is one JSON object with optional
        ``message.content`` chunk, optional ``message.tool_calls``,
        and a final line carrying ``done=true`` plus
        ``prompt_eval_count`` / ``eval_count`` token counters.

        Tool-call accumulation strategy: we keep the latest non-empty
        ``tool_calls`` payload from any chunk. Ollama's streaming
        usually emits the complete call object once per call (not
        char-by-char), so the last value is the canonical batch.
        Empty arrays are ignored so a late "no more tool calls"
        signal doesn't blank a real call we already captured.
        """
        text_parts: list[str] = []
        latest_tool_calls: list[dict[str, Any]] = []
        final_meta: dict[str, Any] = {}

        try:
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug("ollama: skipping malformed NDJSON line: %r", raw)
                    continue

                msg = chunk.get("message") or {}
                content = msg.get("content") or ""
                if content:
                    on_text_delta(content)
                    text_parts.append(content)
                tc = msg.get("tool_calls")
                if tc:
                    latest_tool_calls = tc

                if chunk.get("done"):
                    final_meta = chunk
        finally:
            try:
                resp.close()
            except Exception:
                pass

        synthetic = dict(final_meta)
        synthetic["message"] = {
            "role": "assistant",
            "content": "".join(text_parts),
            "tool_calls": latest_tool_calls,
        }
        return self._build_response(synthetic)

    def _to_ollama(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        if message["role"] == "user":
            if "tool_results" in message:
                return [
                    {
                        "role": "tool",
                        "tool_name": r.get("name", ""),
                        "content": r.get("content") or "",
                    }
                    for r in message["tool_results"]
                ]
            return [{"role": "user", "content": message["content"]}]

        text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []

        if tool_calls and text:
            # Mixed prose+tool_call turn: inline calls via family envelope and drop structured tool_calls so templates don't silently drop one channel.
            inlined = self.dialect.render_tool_calls_in_content(tool_calls)
            return [{"role": "assistant", "content": f"{text}\n{inlined}"}]

        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["args"],
                    },
                }
                for tc in tool_calls
            ]
        return [msg]
