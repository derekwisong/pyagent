"""Text and JSON renderers for `AuditReport`.

Kept separate from `sessions_audit.py` so the data layer stays
testable without console output and so a future TUI / web view can
render the same report without touching JSON-serialization concerns.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Iterable

from pyagent import pricing
from pyagent.sessions_audit import AuditReport, _total_tokens

ALL_SECTIONS = ("cost", "turns", "attachments", "bloat")


def _humanize_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "K", "M", "G"):
        if f < 1024:
            return f"{f:.1f}{unit}" if unit != "B" else f"{int(f)}{unit}"
        f /= 1024
    return f"{f:.1f}T"


def _humanize_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n}"


def _format_cost(cost: float | None) -> str:
    if cost is None:
        return "(unknown — model not in pricing table)"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.3f}"


def render_text(
    report: AuditReport,
    *,
    sections: Iterable[str] | None = None,
    top: int = 20,
    quiet: bool = False,
) -> str:
    """Render the audit as plain text. `sections` narrows to a subset of
    {"cost","turns","attachments","bloat"}; default = all four."""
    sec = set(sections) if sections else set(ALL_SECTIONS)
    lines: list[str] = []

    # Header is always shown — it's the orientation block, not a
    # section. Only the four detail sections respect the filter.
    tokens = report.total_tokens
    total_all = (
        tokens.get("input", 0)
        + tokens.get("output", 0)
        + tokens.get("cache_creation", 0)
        + tokens.get("cache_read", 0)
    )
    lines.append(f"session: {report.session_id}")
    lines.append(f"model:   {report.model or '(none)'}")
    lines.append(f"turns:   {report.turn_count}")
    lines.append(
        "tokens:  {total} total (input {i} / output {o} / "
        "cache_creation {cw} / cache_read {cr})".format(
            total=_humanize_tokens(total_all),
            i=_humanize_tokens(tokens.get("input", 0)),
            o=_humanize_tokens(tokens.get("output", 0)),
            cw=_humanize_tokens(tokens.get("cache_creation", 0)),
            cr=_humanize_tokens(tokens.get("cache_read", 0)),
        )
    )
    lines.append(f"cost:    {_format_cost(report.total_cost_usd)}")
    if report.cost_is_lower_bound and not quiet:
        # Count assistant turns whose cache fields were missing.
        # Approximated by per-turn rows where both cache values are
        # zero AND the report was flagged — close enough for the
        # warning. (We don't store the per-turn presence flag.)
        lines.append(
            "[!] cost is a LOWER BOUND — at least one assistant turn "
            "predates cache logging."
        )

    if "turns" in sec:
        lines.append("")
        lines.append("PER-TURN BREAKDOWN")
        lines.append(
            "  {:>3s}  {:>7s}  {:>7s}  {:>9s}  {:>9s}  {:>10s}".format(
                "#", "input", "output", "cache_w", "cache_r", "cost"
            )
        )
        for t in report.per_turn:
            cost_s = _format_cost(t.cost_usd) if t.cost_usd is not None else "—"
            lines.append(
                "  {:>3d}  {:>7s}  {:>7s}  {:>9s}  {:>9s}  {:>10s}".format(
                    t.turn_idx,
                    f"{t.input:,}",
                    f"{t.output:,}",
                    f"{t.cache_creation:,}",
                    f"{t.cache_read:,}",
                    cost_s,
                )
            )

    if "attachments" in sec:
        lines.append("")
        on_disk = sum(a.size_bytes for a in report.attachments)
        lines.append(
            f"ATTACHMENTS ({len(report.attachments)} files, "
            f"{_humanize_size(on_disk)} on disk)"
        )
        if not report.attachments:
            lines.append("  (none)")
        for a in report.attachments:
            tag = "" if a.ref_count > 0 else "  [orphan]"
            lines.append(
                f"  {a.filename:40s}  {_humanize_size(a.size_bytes):>8s}  "
                f"refs={a.ref_count}{tag}"
            )
        if report.orphan_attachments:
            lines.append(
                f"  ({len(report.orphan_attachments)} orphan(s) — "
                f"not referenced by any turn)"
            )

    if "bloat" in sec:
        lines.append("")
        lines.append("INLINE BLOAT (largest tool results NOT offloaded)")
        if not report.inline_bloat:
            lines.append("  (none)")
        else:
            shown = report.inline_bloat[:top]
            for b in shown:
                lines.append(
                    f"  turn={b.turn_idx:<3d} {b.char_count:>7,d} chars  "
                    f"{b.tool_name}"
                )
                if b.preview:
                    lines.append(f"       {b.preview[:80]}")
            if len(report.inline_bloat) > top:
                lines.append(
                    f"  (+{len(report.inline_bloat) - top} more not shown; "
                    f"raise --top to see more)"
                )

    return "\n".join(lines)


def render_json(report: AuditReport) -> str:
    """Render the report as pretty-printed JSON."""
    return json.dumps(asdict(report), indent=2)


__all__ = ["render_text", "render_json", "ALL_SECTIONS"]
