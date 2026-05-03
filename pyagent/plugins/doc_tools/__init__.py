"""doc-tools — bundled plugin: sub-LLM document extract / summarize.

Two stateless tools, both single-shot sub-LLM calls over one document:

  - ``extract_doc(path, query, schema=None, model=None)`` — pull
    structured fields / lists / tables out of a document. Use when
    the agent wants specific information from a doc larger than ~4KB;
    for smaller docs, ``read_file`` is strictly cheaper.
  - ``summarize_doc(path, focus=None, max_chars=1500, model=None)`` —
    compress a document to prose. Same scale guidance.

The motivation: the main agent reasoning over a 40KB markdown file via
``read_file`` slices burns turns paginating through previews. Delegating
to a focused sub-LLM that returns just the answer keeps those bytes off
the main conversation.

Configuration (all optional, all read at call time):

  ``[plugins.doc-tools]`` table in pyagent's config TOML
    ``model`` — provider/model string the tools call. Defaults to
        ``anthropic/claude-haiku-4-5-20251001``: cheap, fast,
        tool-capable. Local users can switch to ollama with e.g.
        ``model = "ollama/llama3.2:latest"`` (the empirical winner
        of an 8-model eval; see PR #84 for the table).
    ``min_size_chars`` — files smaller than this trigger a "just
        read it directly" response instead of spinning up a sub-LLM.
        Defaults to 4000.
    ``timeout_s`` — wall-clock cap on a single sub-LLM call.
        Defaults to 300 (5 minutes). Hung daemons / wedged remote
        providers surface as ``<… error: sub-LLM call timed out …>``
        instead of blocking the agent loop indefinitely.
    ``cache_size`` — number of cached results to retain
        (process-local, LRU). Defaults to 64. Set to 0 to disable.

  ``PYAGENT_DOC_TOOLS_MODEL`` env var
    Same shape as the config ``model`` value. Useful for
    per-shell overrides without touching config.toml — testing,
    CI, ad-hoc invocation.

Resolution order, highest priority first:
  1. Per-call ``model=`` argument.
  2. ``PYAGENT_DOC_TOOLS_MODEL`` env var.
  3. ``[plugins.doc-tools] model`` in config.toml.
  4. Hardcoded default (haiku).

A model fallback chain (``model = ["ollama/...", "anthropic/..."]``)
is intentionally not implemented in v0.1. See the doc_tools follow-up
issue for the design.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from pyagent import permissions

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"
_DEFAULT_MIN_SIZE_CHARS = 4000
_DEFAULT_TIMEOUT_S = 300
_DEFAULT_CACHE_SIZE = 64
_MODEL_ENV_VAR = "PYAGENT_DOC_TOOLS_MODEL"

# Cap how much of the document we send to the sub-LLM in one call.
# Most modern small models handle ~200K context, but the cost scales
# with input length and the user is paying per call. 200K chars is
# generous for almost any single document; if a user really needs to
# extract from a megabyte of text, that's a different shape (chunk +
# map-reduce) we can build later.
_MAX_DOC_CHARS = 200_000

# Schema strings get embedded in the user prompt verbatim. Cap so a
# pathological caller can't blow up the context window from this
# argument alone. Real JSON Schemas are rarely larger than a few KB.
_MAX_SCHEMA_CHARS = 16_000


_EXTRACT_SYSTEM = (
    "You are an extraction worker. Given a document and an extraction "
    "request, return ONLY the requested information — no preamble, no "
    "commentary, no apology. If the request implies structured data "
    "(a list, a table, fields), output JSON. Otherwise output terse "
    "plain text. If the document does not contain what was asked, "
    "return the literal token <not found> and nothing else."
)

_SUMMARIZE_SYSTEM = (
    "You are a summarization worker. Produce a concise summary of the "
    "document. No preamble, no meta-commentary. If a focus is given, "
    "lead with that aspect. Stay under the requested character budget."
)


# Process-local LRU cache. Keys are tuples that include path
# + mtime_ns + size, so a touched/edited file naturally invalidates.
# Errors are *not* cached — a flaky network shouldn't poison repeats.
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = threading.Lock()


def _read_doc(path: str) -> tuple[str, str | None]:
    """Read `path` for sub-LLM consumption.

    Returns (text, error). On success error is None. On any failure,
    text is empty and error is a `<...>` marker the tool can return
    to the agent verbatim.
    """
    p = Path(path)
    if not permissions.require_access(p):
        return "", f"<access denied: {path}>"
    try:
        text = p.read_text()
    except FileNotFoundError:
        return "", f"<file not found: {path}>"
    except IsADirectoryError:
        return "", f"<is a directory, not a file: {path}>"
    except PermissionError:
        return "", f"<permission denied: {path}>"
    except UnicodeDecodeError:
        # Could be Latin-1, UTF-16, or genuinely binary. We can't
        # tell from one decode failure, so don't claim "binary."
        return "", f"<could not decode as text (not utf-8): {path}>"
    except OSError as e:
        return "", f"<could not read {path}: {e}>"
    return text, None


def _resolve_model(plugin_cfg: dict, override: str | None) -> str:
    """Pick the model string for this call.

    Order: per-call override → env var → plugin config → hardcoded default.
    Env beats config so a user can switch models per-shell without
    editing config.toml — the typical "let me try this with X real
    quick" workflow.
    """
    if override:
        return str(override)
    env_model = os.environ.get(_MODEL_ENV_VAR, "").strip()
    if env_model:
        return env_model
    cfg_model = plugin_cfg.get("model")
    if isinstance(cfg_model, str) and cfg_model.strip():
        return cfg_model.strip()
    return _DEFAULT_MODEL


def _resolve_min_size(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("min_size_chars")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return _DEFAULT_MIN_SIZE_CHARS


def _resolve_timeout(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("timeout_s")
    if isinstance(raw, int) and raw > 0:
        return raw
    return _DEFAULT_TIMEOUT_S


def _resolve_cache_size(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("cache_size")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return _DEFAULT_CACHE_SIZE


def _config_warnings(plugin_cfg: dict) -> list[str]:
    """Sanity-check the ``[plugins.doc-tools]`` table at register time.

    Returns a list of human-readable warning strings — the caller logs
    them via ``api.log("warning", ...)``. None of these warnings block
    registration: the silent-fallback resolvers (``_resolve_*``) still
    promote bogus values to defaults at call time. The point is fast
    feedback at startup so a typo in config.toml doesn't sit unnoticed
    until the agent next invokes the tool.

    Network probes (e.g. "is the configured ollama daemon reachable?")
    are deliberately *not* done here — the configured model is one of
    four resolution sources, so an unreachable default doesn't
    necessarily mean the tool will fail. See the discussion in PR #84.
    """
    out: list[str] = []

    # Model: must be a non-empty string. If it has a `/`, both halves
    # must be non-empty. If it's a bare name (the "shorthand" form,
    # e.g. ``--model anthropic`` → defaults applied), it must match a
    # built-in provider — bare strings that look like model names
    # without a provider prefix are the most common config typo.
    raw_model = plugin_cfg.get("model")
    if raw_model is not None:
        if not isinstance(raw_model, str):
            out.append(
                f"model must be a string, got "
                f"{type(raw_model).__name__}: {raw_model!r}"
            )
        else:
            stripped = raw_model.strip()
            if not stripped:
                out.append("model is set but empty / whitespace-only")
            elif "/" in stripped:
                provider, _, name = stripped.partition("/")
                if not provider.strip() or not name.strip():
                    out.append(
                        f"model {raw_model!r} has empty provider or "
                        f"model name (expected 'provider/model')"
                    )
            else:
                # Bare provider name. We can only verify against
                # built-ins here because plugin-registered providers
                # (e.g. ``ollama``) populate after every plugin's
                # register() runs. Accept silently for shorthand the
                # user may be using on purpose (rare in config), warn
                # only when it's clearly off.
                from pyagent import llms

                builtins = {p.name for p in llms.PROVIDERS}
                if stripped not in builtins and stripped != "ollama":
                    out.append(
                        f"model {raw_model!r} has no '/' separator and "
                        f"doesn't match a known provider; expected "
                        f"'provider/model' form"
                    )

    # timeout_s: positive integer.
    if "timeout_s" in plugin_cfg:
        raw = plugin_cfg["timeout_s"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            out.append(
                f"timeout_s must be a positive integer, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_TIMEOUT_S}"
            )
        elif raw <= 0:
            out.append(
                f"timeout_s must be > 0, got {raw} — using default "
                f"{_DEFAULT_TIMEOUT_S}"
            )

    # cache_size: non-negative integer (0 disables).
    if "cache_size" in plugin_cfg:
        raw = plugin_cfg["cache_size"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            out.append(
                f"cache_size must be a non-negative integer, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_CACHE_SIZE}"
            )
        elif raw < 0:
            out.append(
                f"cache_size must be >= 0, got {raw} — using default "
                f"{_DEFAULT_CACHE_SIZE}"
            )

    # min_size_chars: non-negative integer.
    if "min_size_chars" in plugin_cfg:
        raw = plugin_cfg["min_size_chars"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            out.append(
                f"min_size_chars must be a non-negative integer, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_MIN_SIZE_CHARS}"
            )
        elif raw < 0:
            out.append(
                f"min_size_chars must be >= 0, got {raw} — using "
                f"default {_DEFAULT_MIN_SIZE_CHARS}"
            )

    return out


def _validate_schema(schema: Any) -> tuple[str, str | None]:
    """Validate the optional schema arg. Returns (clean, error).

    Bad inputs return (empty, marker). A None / empty / whitespace
    schema is treated as "no schema" and returns ("", None) — caller
    skips the schema block entirely.
    """
    if schema is None:
        return "", None
    if not isinstance(schema, str):
        return (
            "",
            f"<error: schema must be a string, got {type(schema).__name__}>",
        )
    s = schema.strip()
    if not s:
        return "", None
    if len(s) > _MAX_SCHEMA_CHARS:
        return (
            "",
            f"<error: schema is {len(s)} chars (max {_MAX_SCHEMA_CHARS})>",
        )
    try:
        json.loads(s)
    except json.JSONDecodeError as e:
        return "", f"<error: schema is not valid JSON: {e}>"
    return s, None


def _file_signature(path: str) -> tuple[str, int, int] | None:
    """Return (resolved_path, mtime_ns, size) or None if the file
    can't be stat'd. Used as the file-content slice of the cache key
    — touch / edit invalidates naturally because mtime changes."""
    try:
        p = Path(path).resolve()
        st = p.stat()
    except OSError:
        return None
    return str(p), st.st_mtime_ns, st.st_size


def _cache_get(key: tuple) -> str | None:
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def _cache_put(key: tuple, value: str, max_size: int) -> None:
    if max_size <= 0:
        return
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > max_size:
            _cache.popitem(last=False)


def _cache_clear() -> None:
    """Drop all cached entries. Test hook; not exposed as a tool."""
    with _cache_lock:
        _cache.clear()


def _call_subllm(
    model: str, system: str, user: str, timeout_s: int
) -> tuple[str, str]:
    """Run one sub-LLM turn. Returns (text, error).

    Error is empty on success. On any failure, text is empty and
    error is a short reason the tool wraps in its own marker.

    Timeout is enforced via a daemon thread we join with a deadline.
    This is not concurrency — it's a kill-able wrapper around one
    blocking I/O call. Daemon=True so a stalled call doesn't block
    Python's exit handlers when the user Ctrl-C's the agent.
    """
    # Deferred import: keeps pyagent.llms out of plugin-load critical
    # path for users who never invoke doc-tools.
    from pyagent import llms

    try:
        client = llms.get_client(model)
    except Exception as e:
        return "", f"could not load model {model!r}: {e}"

    box: dict[str, Any] = {}

    def _do_call() -> None:
        try:
            box["result"] = client.respond(
                conversation=[{"role": "user", "content": user}],
                system=system,
            )
        except Exception as e:  # noqa: BLE001 — surface to the tool result
            box["error"] = e

    t = threading.Thread(target=_do_call, daemon=True)
    t.start()
    t.join(timeout=timeout_s)

    if t.is_alive():
        # The daemon thread is still running. We can't cancel it —
        # Python doesn't have safe thread-kill — but daemon=True means
        # the orphaned worker won't block process exit. The user's
        # call gets a clean timeout marker; the LLM client's own HTTP
        # connection will time out on its own schedule.
        return "", f"sub-LLM call timed out after {timeout_s}s ({model!r})"
    if "error" in box:
        return "", f"sub-LLM call failed ({model!r}): {box['error']}"

    result = box.get("result")
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        return "", f"sub-LLM returned no text ({model!r})"
    return text, ""


def register(api):
    # Lightweight register-time validation of the [plugins.doc-tools]
    # table. Bogus values still fall through to defaults at call time
    # (via the _resolve_* helpers) — this is purely about surfacing
    # config typos at startup instead of letting them sit silent.
    for warning in _config_warnings(api.plugin_config or {}):
        api.log("warning", warning)

    def extract_doc(
        path: str,
        query: str,
        schema: str | None = None,
        model: str | None = None,
    ) -> str:
        """Extract structured info from a document via a sub-LLM.

        Use this for documents larger than a few KB when you want
        specific fields, lists, or tables. The sub-LLM reads the whole
        document and returns just the answer — far fewer turns than
        slicing through previews with ``read_file``. For small files
        (<4KB by default), this tool refuses and tells you to read
        directly.

        Results are cached process-locally by (path + mtime + size,
        query, schema, resolved-model), so identical re-queries of an
        unchanged document return instantly without re-spending a
        sub-LLM call. Touching or editing the file invalidates.

        Args:
            path: File to extract from. Subject to the standard
                permission gate.
            query: What to extract. Be specific. "List each horse with
                name, odds, jockey, trainer as JSON" beats "tell me
                about the horses". Vague queries produce vague results.
            schema: Optional JSON schema string. When provided, the
                extractor is told to return strict JSON conforming
                to the schema. Validated as parseable JSON (rejected
                on parse error) and capped at 16K chars.
            model: Optional ``provider/model`` override. Defaults to
                the configured model
                (``[plugins.doc-tools] model`` in config.toml).

        Returns:
            The extracted text or JSON, prefixed with the model that
            produced it. Errors return as ``<extract error: ...>``.
        """
        if not isinstance(path, str) or not path.strip():
            return "<error: path is required>"
        if not isinstance(query, str) or not query.strip():
            return "<error: query is required — describe what to extract>"
        clean_schema, schema_err = _validate_schema(schema)
        if schema_err is not None:
            return schema_err

        plugin_cfg = api.plugin_config or {}
        min_size = _resolve_min_size(plugin_cfg)
        text, err = _read_doc(path)
        if err is not None:
            return err
        if len(text) < min_size:
            return (
                f"<file is {len(text)} chars (under {min_size}-char "
                f"threshold); read_file directly with start/end is "
                f"strictly cheaper than spinning up a sub-LLM>"
            )
        if len(text) > _MAX_DOC_CHARS:
            return (
                f"<file is {len(text)} chars (over {_MAX_DOC_CHARS}-char "
                f"limit); slice with read_file or grep first, then "
                f"call extract_doc on a narrower range>"
            )

        resolved_model = _resolve_model(plugin_cfg, model)
        sig = _file_signature(path)
        cache_size = _resolve_cache_size(plugin_cfg)
        cache_key: tuple | None = None
        if sig is not None and cache_size > 0:
            cache_key = ("extract_doc", sig, query, clean_schema, resolved_model)
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        user_parts = [f"Document path: {path}", "Document content:", text, ""]
        if clean_schema:
            user_parts.append(
                f"Return JSON matching this schema:\n{clean_schema}"
            )
        user_parts.append(f"Extraction request: {query}")
        user = "\n".join(user_parts)

        timeout_s = _resolve_timeout(plugin_cfg)
        out, err_str = _call_subllm(
            resolved_model, _EXTRACT_SYSTEM, user, timeout_s
        )
        if err_str:
            return f"<extract error: {err_str}>"
        result_str = f"[extracted via {resolved_model}]\n{out}"
        if cache_key is not None:
            _cache_put(cache_key, result_str, cache_size)
        return result_str

    def summarize_doc(
        path: str,
        focus: str | None = None,
        max_chars: int = 1500,
        model: str | None = None,
    ) -> str:
        """Summarize a document via a sub-LLM.

        Use this when you want the gist of a doc larger than a few
        KB without dragging the whole text through your context.
        For small files, ``read_file`` directly is cheaper.

        Results are cached process-locally by (path + mtime + size,
        focus, max_chars, resolved-model). Identical re-queries of an
        unchanged document return instantly. Touching or editing the
        file invalidates.

        Args:
            path: File to summarize. Subject to the standard
                permission gate.
            focus: Optional aspect to emphasize (e.g., "financial
                figures", "deadlines", "API surface"). The summary
                will lead with this if specified.
            max_chars: Target length for the summary, in characters.
                Default 1500. The sub-LLM is asked to stay under this;
                it may slightly overshoot.
            model: Optional ``provider/model`` override. Defaults to
                the configured model
                (``[plugins.doc-tools] model`` in config.toml).

        Returns:
            The summary text, prefixed with the model that produced
            it. Errors return as ``<summarize error: ...>``.
        """
        if not isinstance(path, str) or not path.strip():
            return "<error: path is required>"
        try:
            max_chars_int = int(max_chars)
        except (TypeError, ValueError):
            return f"<error: max_chars must be an integer, got {max_chars!r}>"
        if max_chars_int < 100:
            return (
                f"<error: max_chars={max_chars_int} too small; "
                f"minimum 100>"
            )

        plugin_cfg = api.plugin_config or {}
        min_size = _resolve_min_size(plugin_cfg)
        text, err = _read_doc(path)
        if err is not None:
            return err
        if len(text) < min_size:
            return (
                f"<file is {len(text)} chars (under {min_size}-char "
                f"threshold); read_file directly is cheaper than "
                f"spinning up a sub-LLM>"
            )
        if len(text) > _MAX_DOC_CHARS:
            return (
                f"<file is {len(text)} chars (over {_MAX_DOC_CHARS}-char "
                f"limit); slice with read_file first>"
            )

        resolved_model = _resolve_model(plugin_cfg, model)
        sig = _file_signature(path)
        cache_size = _resolve_cache_size(plugin_cfg)
        cache_key: tuple | None = None
        focus_norm = (focus or "").strip()
        if sig is not None and cache_size > 0:
            cache_key = (
                "summarize_doc", sig, focus_norm, max_chars_int, resolved_model,
            )
            cached = _cache_get(cache_key)
            if cached is not None:
                return cached

        user_parts = [
            f"Document path: {path}",
            "Document content:",
            text,
            "",
            f"Summary budget: under {max_chars_int} characters.",
        ]
        if focus_norm:
            user_parts.append(f"Focus on: {focus_norm}")
        user_parts.append("Produce the summary now.")
        user = "\n".join(user_parts)

        timeout_s = _resolve_timeout(plugin_cfg)
        out, err_str = _call_subllm(
            resolved_model, _SUMMARIZE_SYSTEM, user, timeout_s
        )
        if err_str:
            return f"<summarize error: {err_str}>"
        result_str = f"[summarized via {resolved_model}]\n{out}"
        if cache_key is not None:
            _cache_put(cache_key, result_str, cache_size)
        return result_str

    api.register_tool("extract_doc", extract_doc)
    api.register_tool("summarize_doc", summarize_doc)
