"""Per-session token / cost / bloat audit.

Pure data layer — reads `conversation.jsonl` and the `attachments/`
dir, returns an `AuditReport`. No console I/O, no click. Renderers
live in `pyagent.sessions_audit_render`.

The audit answers four questions about a saved session:
  1. What did it cost (cumulative, per-turn)?
  2. Where are the tokens going (input vs output vs cache)?
  3. Which attachments are referenced vs orphaned?
  4. Which inline tool results are bloating the prompt?

Costs estimated via `pyagent.pricing`. Sessions saved before #15 (the
cache-token-logging PR) lack `cache_creation` / `cache_read` in their
usage dicts; the report flags that case via `cost_is_lower_bound = True`
so the renderer can warn the human.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyagent import pricing


# Matches the prefix produced by `Agent._format_offload_ref(path, size, preview)`.
# Captures the attachment path and its char count. Anchored to the start
# of the tool-result content; unanchored matching would catch the
# substring inside an inline tool result that happens to mention an
# attachment, inflating the offload count.
_OFFLOAD_RE = re.compile(r"^\[output saved to (\S+) \((\d+) chars\)")


@dataclass
class TurnRow:
    turn_idx: int
    input: int
    output: int
    cache_creation: int
    cache_read: int
    cost_usd: float | None


@dataclass
class AttachmentRow:
    filename: str
    size_bytes: int
    ref_count: int


@dataclass
class BloatRow:
    turn_idx: int
    tool_name: str
    char_count: int
    preview: str  # first ~200 chars, newlines collapsed


@dataclass
class AuditReport:
    session_id: str
    model: str
    turn_count: int
    total_tokens: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    cost_is_lower_bound: bool = False
    per_turn: list[TurnRow] = field(default_factory=list)
    attachments: list[AttachmentRow] = field(default_factory=list)
    orphan_attachments: list[str] = field(default_factory=list)
    inline_bloat: list[BloatRow] = field(default_factory=list)


def _total_tokens(model: str, t: TurnRow) -> int:
    """Per-turn token total mirroring `pricing.format_usage_suffix`'s gate.

    Anthropic: bundle all four (the four counts are disjoint).
    Other providers: input + output only (their `input` already
    includes cached tokens, so adding cache_read would double-count).
    """
    name = pricing.model_name(model)
    if pricing.is_anthropic_model(name):
        return t.input + t.output + t.cache_creation + t.cache_read
    return t.input + t.output


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _collapse_preview(s: str, n: int = 200) -> str:
    snippet = s[:n].replace("\n", " ").replace("\r", " ")
    while "  " in snippet:
        snippet = snippet.replace("  ", " ")
    return snippet.strip()


def audit_session(
    session_dir: Path, *, model: str | None = None, top_bloat: int = 20
) -> AuditReport:
    """Read `session_dir/conversation.jsonl` + `attachments/` and build a report.

    `model` is the pricing model. `None` falls back to a generic
    placeholder (`""`) which makes `pricing.estimate_cost_usd` return
    None — the report still has token totals, just no $ figure.
    """
    model = model or ""
    session_id = session_dir.name
    conv_path = session_dir / "conversation.jsonl"
    attach_dir = session_dir / "attachments"

    entries = _iter_jsonl(conv_path)

    per_turn: list[TurnRow] = []
    inline_bloat: list[BloatRow] = []
    attachment_refs: dict[str, int] = {}
    cost_is_lower_bound = False

    # turn_idx counts user→assistant exchanges by assistant-turn order
    # (1-indexed). Inline-bloat rows are tagged with the turn the tool
    # result LANDED in, which is the next assistant turn (since tool
    # results are in a user-role message that precedes the assistant's
    # follow-up). Approximation: we tag with the assistant index just
    # seen, which is what the human cares about for "blame which turn".
    last_assistant_idx = 0
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role == "assistant":
            usage = entry.get("usage") or {}
            if "cache_creation" not in usage or "cache_read" not in usage:
                # Pre-#15 transcript missing cache fields. Token totals
                # still meaningful (input/output present), but the cost
                # estimate is a lower bound — cache writes/reads cost
                # real money on Anthropic.
                cost_is_lower_bound = True
            input_t = int(usage.get("input", 0) or 0)
            output_t = int(usage.get("output", 0) or 0)
            cache_w = int(usage.get("cache_creation", 0) or 0)
            cache_r = int(usage.get("cache_read", 0) or 0)
            totals["input"] += input_t
            totals["output"] += output_t
            totals["cache_creation"] += cache_w
            totals["cache_read"] += cache_r
            last_assistant_idx += 1
            cost = pricing.estimate_cost_usd(
                model, input_t, output_t, cache_w, cache_r
            )
            per_turn.append(
                TurnRow(
                    turn_idx=last_assistant_idx,
                    input=input_t,
                    output=output_t,
                    cache_creation=cache_w,
                    cache_read=cache_r,
                    cost_usd=cost,
                )
            )
            continue
        if role == "user":
            tool_results = entry.get("tool_results")
            if not isinstance(tool_results, list):
                continue
            for tr in tool_results:
                if not isinstance(tr, dict):
                    continue
                content = tr.get("content")
                if not isinstance(content, str):
                    continue
                name = tr.get("name", "?")
                m = _OFFLOAD_RE.match(content)
                if m:
                    attachment_refs[m.group(1)] = (
                        attachment_refs.get(m.group(1), 0) + 1
                    )
                    continue
                inline_bloat.append(
                    BloatRow(
                        turn_idx=last_assistant_idx,
                        tool_name=name,
                        char_count=len(content),
                        preview=_collapse_preview(content),
                    )
                )

    inline_bloat.sort(key=lambda r: r.char_count, reverse=True)
    inline_bloat = inline_bloat[:top_bloat]

    # Attachments: list every file in attachments/ and tag with the
    # ref count from the tool-result scan. Files with ref_count == 0
    # are orphans (tool ran, but the attachment is no longer
    # referenced — usually because the turn was rolled back).
    attachments: list[AttachmentRow] = []
    orphans: list[str] = []
    if attach_dir.exists():
        for f in sorted(attach_dir.iterdir()):
            if not f.is_file():
                continue
            # The offload prefix uses the path as written by the agent.
            # Match by suffix `attachments/<name>` so a relative or
            # absolute path both hit. Mirrors `Session.find_orphan_attachments`.
            ref_count = 0
            for ref_path, count in attachment_refs.items():
                if ref_path.endswith(f"attachments/{f.name}") or ref_path.endswith(f.name):
                    ref_count += count
            row = AttachmentRow(
                filename=f.name,
                size_bytes=f.stat().st_size,
                ref_count=ref_count,
            )
            attachments.append(row)
            if ref_count == 0:
                orphans.append(f.name)

    total_cost_usd = pricing.estimate_cost_usd(
        model,
        totals["input"],
        totals["output"],
        totals["cache_creation"],
        totals["cache_read"],
    )

    return AuditReport(
        session_id=session_id,
        model=model,
        turn_count=last_assistant_idx,
        total_tokens=totals,
        total_cost_usd=total_cost_usd,
        cost_is_lower_bound=cost_is_lower_bound,
        per_turn=per_turn,
        attachments=attachments,
        orphan_attachments=orphans,
        inline_bloat=inline_bloat,
    )
