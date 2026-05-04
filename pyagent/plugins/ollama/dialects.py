"""Per-model-family wire-format quirks for Ollama-hosted models.

Ollama isn't an API — it's a runtime hosting many model families,
each with its own chat template and its own conventions for
serializing tool calls. Ollama's ``/api/chat`` accepts a structured
``tool_calls`` field and the model's template renders it in the
right native envelope, so for tool-call-only assistant turns we have
nothing to do — Ollama gets it right.

The dialect only matters for **mixed turns** — assistant messages
that carry both prose and a tool call. Two real, opposite problems
show up there depending on which template the model ships with:

  - Qwen-style templates render ``.Content`` OR ``.ToolCalls`` but
    never both: when ``.Content`` is non-empty, ``.ToolCalls``
    silently disappears from the rendered prompt. Next turn's
    tool_response then arrives with no preceding ``<tool_call>``
    envelope and the model loses the call/response thread.
  - Llama-style templates have the opposite precedence — ``.ToolCalls``
    win, ``.Content`` is dropped. Same information loss, opposite
    direction (prose vanishes instead of the call).

The fix in both directions is to inline the call into ``content``
ourselves, in whichever envelope the model was actually trained to
recognize, then omit the structured ``tool_calls`` field so the
template's else-branch renders our hand-built content unchanged.

Detection is by template-string sniffing. We don't maintain a
model-name registry (Ollama users keep pulling new tags, custom
Modelfiles, fine-tunes, etc.) — instead we pattern-match the
template itself for the literal envelope tokens each family bakes
in. A non-matching template falls back to the qwen-style envelope
because that's the most common ChatML convention among modern
open models (Hermes, qwen, many fine-tunes).
"""

from __future__ import annotations

import json
from typing import Any


class Dialect:
    """Base class. Subclasses override :meth:`render_tool_calls_in_content`
    with the model family's native in-content tool-call envelope."""

    name: str = "default"

    def render_tool_calls_in_content(
        self, tool_calls: list[dict[str, Any]]
    ) -> str:
        """Return the in-content envelope for the given tool calls.

        Caller is responsible for composing this with any preceding
        prose (a single newline between is conventional).
        """
        raise NotImplementedError


class QwenDialect(Dialect):
    """ChatML-style — ``<tool_call>{...}</tool_call>``, ``arguments`` key.

    Used by qwen2/2.5/3 and Hermes-family fine-tunes. Multiple calls
    in a single turn are stacked one JSON object per line inside one
    ``<tool_call>`` block, matching what the qwen2.5 template emits
    on the ``.ToolCalls`` path.
    """

    name = "qwen"

    def render_tool_calls_in_content(
        self, tool_calls: list[dict[str, Any]]
    ) -> str:
        body = "\n".join(
            json.dumps({"name": tc["name"], "arguments": tc["args"]})
            for tc in tool_calls
        )
        return f"<tool_call>\n{body}\n</tool_call>"


class LlamaDialect(Dialect):
    """Llama 3.1+ style — bare JSON, ``parameters`` key, no wrapper.

    Llama 3.x templates render assistant tool calls as a raw JSON
    object placed at the assistant header position (no ``<tool_call>``
    wrapper) and use ``parameters`` rather than ``arguments`` as the
    args key. Multiple calls render one JSON object per line, matching
    how the ollama llama3.1 template iterates ``.ToolCalls``.
    """

    name = "llama"

    def render_tool_calls_in_content(
        self, tool_calls: list[dict[str, Any]]
    ) -> str:
        return "\n".join(
            json.dumps({"name": tc["name"], "parameters": tc["args"]})
            for tc in tool_calls
        )


def detect_from_template(template: str | None) -> Dialect:
    """Classify an Ollama template string into a Dialect.

    Looks for literal envelope tokens that each family bakes into
    its template — ``<tool_call>`` for qwen-style, ``<|start_header_id|>``
    plus ``"parameters"`` for llama 3.x. Order matters; first hit
    wins. Falls back to :class:`QwenDialect` when nothing matches —
    that's the most common modern ChatML convention, and treating
    it as the default yields the right answer for most fine-tunes
    Ollama users pull.
    """
    if not template:
        return QwenDialect()
    if "<tool_call>" in template:
        return QwenDialect()
    if "<|start_header_id|>" in template and '"parameters"' in template:
        return LlamaDialect()
    return QwenDialect()
