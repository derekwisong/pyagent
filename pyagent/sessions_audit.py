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
    # Number of assistant turns whose `usage` dict lacked cache fields
    # (pre-#15 sessions). The renderer uses this for an "X of Y" warning
    # so the user can judge how much the cost number is missing.
    pre_15_turns: int = 0
    per_turn: list[TurnRow] = field(default_factory=list)
    attachments: list[AttachmentRow] = field(default_factory=list)
    orphan_attachments: list[str] = field(default_factory=list)
    inline_bloat: list[BloatRow] = field(default_factory=list)


def _total_tokens_summary(model: str, totals: dict[str, int]) -> int:
    """Aggregate token total mirroring `pricing.format_usage_suffix`'s gate.

    Anthropic: bundle all four (the four counts are disjoint —
    `input_tokens` excludes cache reads and writes, so the sum is the
    real prompt size). Other providers: input + output only (their
    `prompt_tokens` / `prompt_token_count` already includes cached
    tokens, so adding cache_read would double-count the same tokens).

    Used by the audit-report header and the bench report. Per-turn
    rows in the breakdown table render the four counts separately, so
    no per-turn variant is needed.
    """
    name = pricing.model_name(model)
    if pricing.is_anthropic_model(name):
        return (
            totals.get("input", 0)
            + totals.get("output", 0)
            + totals.get("cache_creation", 0)
            + totals.get("cache_read", 0)
        )
    return totals.get("input", 0) + totals.get("output", 0)


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
    pre_15_turns = 0
    totals = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
    # Per-turn cost runs through the turn's RECORDED model (added to
    # usage by the LLM clients in the bench-followups PR), falling back
    # to the function arg for older sessions. Aggregating per-turn
    # costs (vs. multiplying summed tokens by one model's rates) is the
    # only way to stay correct across a session that spanned multiple
    # models — e.g. the user switched via /model partway through.
    per_turn_costs_sum = 0.0
    any_cost_priced = False
    recorded_models: list[str] = []

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
                pre_15_turns += 1
            input_t = int(usage.get("input", 0) or 0)
            output_t = int(usage.get("output", 0) or 0)
            cache_w = int(usage.get("cache_creation", 0) or 0)
            cache_r = int(usage.get("cache_read", 0) or 0)
            recorded = usage.get("model")
            turn_model = recorded or model
            if recorded:
                recorded_models.append(recorded)
            totals["input"] += input_t
            totals["output"] += output_t
            totals["cache_creation"] += cache_w
            totals["cache_read"] += cache_r
            last_assistant_idx += 1
            cost = pricing.estimate_cost_usd(
                turn_model, input_t, output_t, cache_w, cache_r
            )
            if cost is not None:
                per_turn_costs_sum += cost
                any_cost_priced = True
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
            # The `attachments/` segment anchors the match so a tool
            # result that happens to mention a bare filename can't
            # falsely register as a reference.
            ref_count = 0
            for ref_path, count in attachment_refs.items():
                if ref_path.endswith(f"attachments/{f.name}"):
                    ref_count += count
            row = AttachmentRow(
                filename=f.name,
                size_bytes=f.stat().st_size,
                ref_count=ref_count,
            )
            attachments.append(row)
            if ref_count == 0:
                orphans.append(f.name)

    # Aggregate cost = sum of per-turn costs (correct across mixed
    # models). Falls back to summing-then-pricing only when no turn
    # had a priceable model.
    if any_cost_priced:
        total_cost_usd: float | None = per_turn_costs_sum
    else:
        total_cost_usd = pricing.estimate_cost_usd(
            model,
            totals["input"],
            totals["output"],
            totals["cache_creation"],
            totals["cache_read"],
        )

    # Header model: prefer the most recent turn's recorded model (the
    # session's "current" identity) so a session whose only
    # caller-supplied model was a fallback still surfaces what actually
    # ran. Drop back to the function arg if no turn recorded one.
    header_model = recorded_models[-1] if recorded_models else model

    return AuditReport(
        session_id=session_id,
        model=header_model,
        turn_count=last_assistant_idx,
        total_tokens=totals,
        total_cost_usd=total_cost_usd,
        cost_is_lower_bound=cost_is_lower_bound,
        pre_15_turns=pre_15_turns,
        per_turn=per_turn,
        attachments=attachments,
        orphan_attachments=orphans,
        inline_bloat=inline_bloat,
    )
