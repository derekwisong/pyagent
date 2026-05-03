"""doc-tools — bundled plugin: sub-LLM document extract / summarize.

Two stateless tools, both single-shot sub-LLM calls over one document:

  - ``extract(path, query, schema=None, model=None)`` — pull structured
    fields / lists / tables out of a document. Use when the agent wants
    specific information from a doc larger than ~4KB; for smaller docs,
    ``read_file`` is strictly cheaper.
  - ``summarize(path, focus=None, max_chars=1500, model=None)`` —
    compress a document to prose. Same scale guidance.

The motivation: the main agent reasoning over a 40KB markdown file via
``read_file`` slices burns turns paginating through previews. Delegating
to a focused sub-LLM that returns just the answer keeps those bytes off
the main conversation.

Configuration (all optional, all read at call time):

  ``[plugins.doc-tools]`` table in pyagent's config TOML
    ``model`` — provider/model string the tools call. Defaults to
        ``ollama/llama3.2:latest`` — chosen from a head-to-head over
        local ollama models on representative fixtures: 5s extract,
        sub-1s summarize, perfect schema match, 2GB on disk. Requires
        the ollama daemon running and the model pulled; users without
        ollama should set this to a hosted model
        (e.g. ``anthropic/claude-haiku-4-5-20251001``). Per-call
        ``model=`` overrides.
    ``min_size_chars`` — files smaller than this trigger a "just
        read it directly" response instead of spinning up a sub-LLM.
        Defaults to 4000 — below that the round-trip cost beats
        ``read_file`` + reasoning.

A model fallback chain (``model = ["ollama/...", "anthropic/..."]``)
is intentionally not implemented in v0.1. See the doc_tools follow-up
issue for the design.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pyagent import permissions

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "ollama/llama3.2:latest"
_DEFAULT_MIN_SIZE_CHARS = 4000

# Cap how much of the document we send to the sub-LLM in one call.
# Most modern small models handle ~200K context, but the cost scales
# with input length and the user is paying per call. 200K chars is
# generous for almost any single document; if a user really needs to
# extract from a megabyte of text, that's a different shape (chunk +
# map-reduce) we can build later.
_MAX_DOC_CHARS = 200_000


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
        return "", f"<binary file (not text): {path}>"
    except OSError as e:
        return "", f"<could not read {path}: {e}>"
    return text, None


def _resolve_model(plugin_cfg: dict, override: str | None) -> str:
    if override:
        return str(override)
    cfg_model = plugin_cfg.get("model")
    if isinstance(cfg_model, str) and cfg_model.strip():
        return cfg_model.strip()
    return _DEFAULT_MODEL


def _resolve_min_size(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("min_size_chars")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return _DEFAULT_MIN_SIZE_CHARS


def _call_subllm(model: str, system: str, user: str) -> tuple[str, str]:
    """Run one sub-LLM turn. Returns (text, error).

    Error is empty on success. On any failure, text is empty and
    error is a short reason the tool wraps in its own marker.
    """
    # Deferred import: keeps pyagent.llms out of plugin-load critical
    # path for users who never invoke doc-tools.
    from pyagent import llms

    try:
        client = llms.get_client(model)
    except Exception as e:
        return "", f"could not load model {model!r}: {e}"
    try:
        result = client.respond(
            conversation=[{"role": "user", "content": user}],
            system=system,
        )
    except Exception as e:
        return "", f"sub-LLM call failed ({model!r}): {e}"
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str) or not text.strip():
        return "", f"sub-LLM returned no text ({model!r})"
    return text, ""


def register(api):
    plugin_cfg_getter = lambda: api.plugin_config

    def extract(
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

        Args:
            path: File to extract from. Subject to the standard
                permission gate.
            query: What to extract. Be specific. "List each horse with
                name, odds, jockey, trainer as JSON" beats "tell me
                about the horses". Vague queries produce vague results.
            schema: Optional JSON schema string. When provided, the
                extractor is told to return strict JSON conforming
                to the schema.
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

        plugin_cfg = plugin_cfg_getter() or {}
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
                f"call extract on a narrower range>"
            )

        resolved_model = _resolve_model(plugin_cfg, model)
        user_parts = [f"Document path: {path}", "Document content:", text, ""]
        if schema:
            user_parts.append(
                f"Return JSON matching this schema:\n{schema}"
            )
        user_parts.append(f"Extraction request: {query}")
        user = "\n".join(user_parts)

        out, err_str = _call_subllm(resolved_model, _EXTRACT_SYSTEM, user)
        if err_str:
            return f"<extract error: {err_str}>"
        return f"[extracted via {resolved_model}]\n{out}"

    def summarize(
        path: str,
        focus: str | None = None,
        max_chars: int = 1500,
        model: str | None = None,
    ) -> str:
        """Summarize a document via a sub-LLM.

        Use this when you want the gist of a doc larger than a few
        KB without dragging the whole text through your context.
        For small files, ``read_file`` directly is cheaper.

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

        plugin_cfg = plugin_cfg_getter() or {}
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
        user_parts = [
            f"Document path: {path}",
            "Document content:",
            text,
            "",
            f"Summary budget: under {max_chars_int} characters.",
        ]
        if focus:
            user_parts.append(f"Focus on: {focus}")
        user_parts.append("Produce the summary now.")
        user = "\n".join(user_parts)

        out, err_str = _call_subllm(resolved_model, _SUMMARIZE_SYSTEM, user)
        if err_str:
            return f"<summarize error: {err_str}>"
        return f"[summarized via {resolved_model}]\n{out}"

    api.register_tool("extract", extract)
    api.register_tool("summarize", summarize)
