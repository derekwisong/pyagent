import asyncio
import collections
import io
import logging
import multiprocessing
import re
import shutil
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.traceback import install as install_traceback
from wcwidth import wcswidth

from pyagent import agent_proc
from pyagent import config
from pyagent import llms
from pyagent import paths
from pyagent import permissions
from pyagent import protocol
from pyagent import roles
from pyagent.session import Session

logger = logging.getLogger(__name__)
console = Console()


def _summarize_args(args: dict[str, Any]) -> str:
    for key in ("path", "pattern", "url", "command", "name"):
        if key in args:
            val = str(args[key]).replace("\n", " ")
            if len(val) > 60:
                val = val[:57] + "..."
            return f"{key}={val}"
    return ""


def _agent_label(agent_id: str | None) -> str:
    """Format the per-event label prefix for events from a subagent.

    Returns an empty string for events from the root agent (agent_id
    is None) so the existing single-agent UI is unchanged. For events
    from a subagent, returns a dim cyan tag like `[researcher-3a4f] `
    so the human can tell which subagent produced what.

    The opening bracket is escaped (`\\[`) because rich interprets a
    bare `[name]` inside a markup string as a tag — without the
    escape, the inner brackets get silently swallowed and only the
    trailing space survives. The closing bracket is fine unescaped
    (rich only treats `[` as the start of markup).
    """
    if not agent_id:
        return ""
    return f"[cyan]\\[{agent_id}][/cyan] "


def _on_text(text: str, agent_id: str | None = None) -> None:
    """Render the agent's markdown dim-toned with blank-line breathing
    room above and below.

    Markdown's own emphasis (bold, headers, lists, code) still shows
    through; the `dim` base just nudges the body text a shade down so
    the user's `> ` prompt visually pops compared to the assistant's
    voice. Reflows naturally on terminal resize because rich renders
    each call against the live console width — no captured ANSI.
    """
    console.print()
    if agent_id:
        console.print(_agent_label(agent_id))
    console.print(Markdown(text), style="dim")
    console.print()


# Per-agent streaming state. Keys are agent_id strings (or "root" for
# the main agent); each value is a dict tracking what we've streamed
# so the closing `assistant_text` event can erase it cleanly and
# re-render the same text as markdown (recovering bold/headers/code-
# blocks the streamed plain text loses).
#
# Fields per state entry:
#   - "buffer":   plain text streamed so far (no ANSI). Used to count
#                 visual rows the cursor has advanced.
#   - "width":    terminal width snapshot at the start of streaming.
#                 We don't react to mid-turn resize; the cursor math
#                 would lie if we did.
#   - "header_advances": how many rows the cursor moved down BEFORE the
#                 first chunk (leading blank line + optional agent
#                 label). Tracked separately because it's plain
#                 newlines with no soft-wrap component.
_streaming_state: dict[str, dict[str, Any]] = {}

# Strip ANSI control sequences when measuring visible character count.
# Models occasionally emit `\033[...m` color codes inline; without this
# strip, our per-row width math would treat them as visible chars and
# the cursor-up math would over-count.
_ANSI_RE = re.compile(r"\x1b\[[\d;]*[a-zA-Z]")


def _count_cursor_advance(buffer: str, width: int) -> int:
    """Count how many rows the cursor advanced from where streaming
    started, given the streamed buffer and the terminal width.

    Each `\\n` counts as one row. Each over-width segment between
    newlines contributes ``(visible_len - 1) // width`` extra rows
    from soft-wrapping. ANSI escape sequences are stripped before
    measuring because they don't occupy visual columns.

    Returns 0 for an empty buffer or non-positive width — both mean
    "nothing to clear / nowhere safe to compute," and the caller
    short-circuits the ANSI move.
    """
    if not buffer or width <= 0:
        return 0
    advance = 0
    visible = _ANSI_RE.sub("", buffer)
    segments = visible.split("\n")
    for i, seg in enumerate(segments):
        if i > 0:
            advance += 1  # the newline itself
        if seg:
            advance += (len(seg) - 1) // width
    return advance


def _on_text_delta(chunk: str, agent_id: str | None = None) -> None:
    """Print one streaming text chunk inline, no markdown formatting.

    Markdown rendering happens at end-of-turn — the closing
    `assistant_text` event clears the streamed plain text and
    re-renders the same buffer through `_on_text`, recovering
    bold/headers/code blocks the streamed dim text loses. The brief
    reflow on completion is acceptable for the UX win of seeing
    tokens land in real time AND keeping markdown formatting.

    Each text segment in a turn (one per LLM call before/between/
    after tool batches) gets its own leading blank line + agent
    label exactly once, on the first delta.
    """
    key = agent_id or "root"
    state = _streaming_state.get(key)
    if state is None:
        console.print()
        header_advances = 1
        if agent_id:
            console.print(_agent_label(agent_id))
            header_advances += 1
        state = {
            "buffer": "",
            "width": console.width or 0,
            "header_advances": header_advances,
        }
        _streaming_state[key] = state
    state["buffer"] += chunk
    # `style="dim"` matches the non-streaming render so the streamed
    # text doesn't visually pop while the user's prompt is still the
    # most-prominent thing on screen.
    console.print(chunk, end="", style="dim", soft_wrap=True, highlight=False)


def _on_tool_call(
    name: str, args: dict[str, Any], agent_id: str | None = None
) -> None:
    summary = _summarize_args(args)
    label = f"{name}  {summary}" if summary else name
    console.print(f"{_agent_label(agent_id)}[dim grey42]· {label}[/dim grey42]")


def _on_tool_result(
    name: str, content: str, agent_id: str | None = None
) -> None:
    first = content.splitlines()[0] if content else ""
    if first.startswith("Error:") or first.startswith("<"):
        console.print(
            f"{_agent_label(agent_id)}[dim red]  ↳ {first}[/dim red]"
        )


# Status footer ----------------------------------------------------
#
# The bottom-of-screen `thinking…` line gets a richer rendering once
# subagents are alive: each agent's most-recent activity is shown
# inline, separated by `│`. The single-agent rendering is unchanged
# from before the footer landed — same `thinking…` text — so users
# who don't use subagents see no UI churn.
#
# State is per-CLI-process (one dict, mutated in place by the
# asyncio pipe-reader callback in `_repl_async`). It carries across
# turns so a subagent spawned in turn N is still tracked at turn N+1.

_SPAWN_INFO_RE = re.compile(
    r"spawned subagent (?P<name>\S+) \(id=(?P<sid>\S+), depth=\d+\)"
)
_TERM_INFO_RE = re.compile(
    r"terminated subagent \S+ \(id=(?P<sid>[^)]+)\)"
)


# Pricing math lives in pyagent.pricing now so the audit / bench
# entry points can reuse it without dragging click + readline + rich
# along. The aliases below preserve the private names existing tests
# (smoke_token_meter etc.) import from this module — zero-touch.
from pyagent.pricing import (
    ANTHROPIC_CACHE_READ_MULT as _ANTHROPIC_CACHE_READ_MULT,
    ANTHROPIC_CACHE_WRITE_MULT as _ANTHROPIC_CACHE_WRITE_MULT,
    PRICING_USD_PER_MTOK as _PRICING_USD_PER_MTOK,
    estimate_cost_usd as _estimate_cost_usd,
    format_right_zone as _format_right_zone,
    format_usage_suffix as _format_usage_suffix,
    is_anthropic_model as _is_anthropic_model,
    model_name as _model_name,
)


def _render_models_listing() -> str:
    """Aggregate every provider's advertised models into a printable
    block.

    Built-in providers ship hardcoded lists (zero network, no key
    required). Plugin providers can register a live callable —
    ollama's hits `/api/tags` + per-model `/api/show`. Per-provider
    exceptions are caught upstream in `llms.list_all_models()` and
    rendered as `(unavailable: <reason>)` so a stopped local server
    doesn't silence the rest of the catalog.

    pyagent's agent loop relies on tool-calling, so the renderer
    flags ollama models that explicitly don't advertise the ``tools``
    capability with a yellow ``(no tools — chat only)`` note. Models
    where capabilities is empty (older Ollama servers, or built-ins
    we don't enumerate) get no annotation rather than a misleading
    one. Plugin providers are surfaced only if `plugins.load()` has
    populated them.
    """
    listings = llms.list_all_models()
    plugin_names = set(llms._PLUGIN_PROVIDERS)

    lines: list[str] = [
        "Available models — pass with `--model provider/model`:",
        "",
    ]
    for listing in listings:
        suffix = " (plugin)" if listing.name in plugin_names else ""
        lines.append(f"[bold]{listing.name}[/bold]{suffix}:")
        if listing.error:
            lines.append(f"  [yellow](unavailable: {listing.error})[/yellow]")
        elif not listing.models:
            lines.append("  [dim](no models advertised)[/dim]")
        else:
            for m in listing.models:
                tags: list[str] = []
                if m.name == listing.default_model:
                    tags.append("[dim](default)[/dim]")
                if m.capabilities:
                    tags.append(f"[dim]({', '.join(m.capabilities)})[/dim]")
                    if "tools" not in m.capabilities:
                        tags.append("[yellow](no tools — chat only)[/yellow]")
                tag_str = "  " + " ".join(tags) if tags else ""
                lines.append(f"  - {m.name}{tag_str}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _resolve_model(cli_model: str | None) -> str:
    """Pick a model string. Precedence: --model > config.default_model
    > auto-detect from API-key env vars. Raises a click error if all
    three are empty so the user gets a pointed message instead of a
    "ANTHROPIC_API_KEY is not set" deep in the SDK.
    """
    if cli_model:
        return llms.resolve_model(cli_model)
    cfg_default = (config.load().get("default_model") or "").strip()
    if cfg_default:
        return llms.resolve_model(cfg_default)
    detected = llms.auto_detect_provider()
    if detected:
        return llms.resolve_model(detected.name)
    expected = ", ".join(
        v
        for spec in llms.PROVIDERS
        for v in spec.env_vars
    )
    raise click.UsageError(
        "no model selected and no API-key env var is set.\n"
        f"Set one of: {expected}\n"
        "Or pass --model <provider> (e.g. --model openai),\n"
        "or pin a default with `pyagent-config init` then editing default_model."
    )


def _agents_tokens(agents: dict) -> tuple[int, int, int, int]:
    """Sum input/output/cache-write/cache-read tokens across all
    tracked agents."""
    in_tot = sum(a.get("tokens", {}).get("input", 0) for a in agents.values())
    out_tot = sum(a.get("tokens", {}).get("output", 0) for a in agents.values())
    cw_tot = sum(
        a.get("tokens", {}).get("cache_creation", 0) for a in agents.values()
    )
    cr_tot = sum(
        a.get("tokens", {}).get("cache_read", 0) for a in agents.values()
    )
    return in_tot, out_tot, cw_tot, cr_tot


_CHECKLIST_TITLE_MAX = 40


def _checklist_segment(agents: dict, drop_title: bool = False) -> str:
    """Return the footer's checklist segment ('· N/M · "title"') or empty.

    Reads the most recent checklist snapshot stashed under
    `agents["root"]["checklist"]` by `_update_agents_state`. Drops out
    cleanly when there's no list, or when every task is done — the
    point of the segment is *progress on something live*, not a
    monument to past work.

    `drop_title=True` keeps just the `N/M` count — the third step in
    the footer's degradation pipeline (issue #67).
    """
    cl = agents.get("root", {}).get("checklist")
    if not cl:
        return ""
    total = cl.get("total", 0)
    completed = cl.get("completed", 0)
    if total <= 0 or completed >= total:
        return ""
    title = cl.get("current_title", "") or ""
    if drop_title or not title:
        return f" · {completed}/{total}"
    if len(title) > _CHECKLIST_TITLE_MAX:
        title = title[: _CHECKLIST_TITLE_MAX - 1] + "…"
    return f" · {completed}/{total} · {title}"


def _msgs_segment(
    agents: dict, drop_severity_tag: bool = False
) -> tuple[str, str | None]:
    """Render the footer's `msgs:N` segment from `notes_unread` state.

    Returns `(text, severity)`. `text` is empty when there's nothing
    to show (no notes pending, or count == 0). `severity` is the
    highest severity present (`alert` > `warn` > `info`) or None.

    `drop_severity_tag=True` collapses ` · msgs: 2 (warn)` to
    ` · msgs: 2`. The count survives — only the severity word drops.
    """
    notes = agents.get("root", {}).get("notes_unread")
    if not notes:
        return "", None
    count = int(notes.get("count", 0) or 0)
    if count <= 0:
        return "", None
    by_sev = notes.get("by_severity") or {}
    if int(by_sev.get("alert", 0) or 0) > 0:
        sev = "alert"
    elif int(by_sev.get("warn", 0) or 0) > 0:
        sev = "warn"
    else:
        sev = "info"
    if drop_severity_tag or sev == "info":
        return f" · msgs: {count}", sev
    return f" · msgs: {count} ({sev})", sev


def _agent_count_summary(agents: dict) -> dict[str, int]:
    """Bucket counts for Tier C overflow rendering.

    `ready` and `idle` collapse into the same bucket per the spec:
    no "done" bucket — a finished agent is just idle from the user's
    point of view.
    """
    buckets = {"working": 0, "idle": 0, "error": 0}
    for info in agents.values():
        status = info.get("status", "")
        if status == "error":
            buckets["error"] += 1
        elif status in ("ready", "idle"):
            buckets["idle"] += 1
        else:
            buckets["working"] += 1
    return buckets


def _agents_tier_a(agents: dict) -> str:
    """Tier A — full per-agent labels separated by `│`.

    `root(thinking) │ s1(· bash)`. Plain text (no rich markup) so the
    composer can measure visible width before deciding whether to
    apply Tier B or Tier C collapse.
    """
    parts = []
    for key, info in agents.items():
        label = "root" if key == "root" else key
        parts.append(f"{label}({info.get('status', 'idle')})")
    return " │ ".join(parts)


def _agents_tier_b(agents: dict) -> str:
    """Tier B — drop idle/ready agents, append ` · +N idle`.

    htop-style: keep only the agents doing something interesting.
    Errors stay in the working list (their state is the interesting
    bit). Returns plain text.
    """
    working = []
    idle_n = 0
    for key, info in agents.items():
        label = "root" if key == "root" else key
        status = info.get("status", "")
        if status in ("ready", "idle"):
            idle_n += 1
        else:
            working.append(f"{label}({status})")
    body = " │ ".join(working) if working else ""
    if idle_n > 0:
        sep = " · " if body else ""
        body = f"{body}{sep}+{idle_n} idle"
    return body


def _agents_tier_c(agents: dict) -> str:
    """Tier C — `N agents: X working · Y idle [· Z error]`.

    Zero-count buckets drop out (per spec). The `working` bucket is
    always rendered when non-zero so the user sees what's actually
    in flight; `error` only appears when an agent has actually failed.
    """
    buckets = _agent_count_summary(agents)
    pieces: list[str] = []
    if buckets["working"] > 0:
        pieces.append(f"{buckets['working']} working")
    if buckets["idle"] > 0:
        pieces.append(f"{buckets['idle']} idle")
    if buckets["error"] > 0:
        pieces.append(f"{buckets['error']} error")
    if not pieces:
        # Edge case: every agent in some pre-spawn limbo. Still show
        # the count so the user knows the tree exists.
        pieces.append("0 working")
    return f"{len(agents)} agents: " + " · ".join(pieces)


def _root_status_text(agents: dict) -> str:
    """Single-agent left-zone state word.

    `…` indicates active work — drop it for terminal states
    (`ready`, `error`) so the always-on bottom_toolbar doesn't lie
    about what the agent is doing while it sits idle waiting for the
    next user input.
    """
    status = agents.get("root", {}).get("status", "thinking")
    trailing = "" if status in ("ready", "error") else "…"
    return f"{status}{trailing}"


def _render_status(agents: dict, model: str = "") -> str:
    """Return the rich-markup string for the status footer's left zone.

    Single-agent (only root) → the classic `thinking…` text so the UI
    is unchanged for users not using subagents.

    Multi-agent → `agent(status) │ agent(status) │ …` separated by box-
    drawing pipes. Order is insertion order (root first, then
    subagents in spawn order) which gives a stable left-to-right read.

    Both renderings get a checklist segment appended when the root
    agent has a non-empty, not-yet-finished task list. `model` is
    accepted for API compatibility with earlier versions; the right
    zone (gross/net/$cost) lives in `_format_right_zone_markup` now.
    """
    del model  # right zone is composed separately now
    checklist = _checklist_segment(agents)
    if len(agents) <= 1:
        return f"[dim]{_root_status_text(agents)}{checklist}[/dim]"
    parts = []
    for key, info in agents.items():
        label = "root" if key == "root" else key
        parts.append(f"{label}([cyan]{info['status']}[/cyan])")
    body = " [/dim][dim]│[/dim] [dim]".join(parts)
    return f"[dim]{body}{checklist}[/dim]"


def _format_right_zone_markup(
    agents: dict,
    model: str,
    drop_gross: bool = False,
    drop_net: bool = False,
) -> str:
    """Rich-markup right-zone string: `gross / net · $cost`.

    Empty when no LLM activity has happened yet. `drop_gross` and
    `drop_net` are degradation knobs — gross drops first (step 4),
    then net (step 6). The cost segment never drops.
    """
    in_tot, out_tot, cw_tot, cr_tot = _agents_tokens(agents)
    gross_str, net_str, cost_str = _format_right_zone(
        in_tot, out_tot, model, cw_tot, cr_tot
    )
    if not cost_str:
        return ""
    pieces: list[str] = []
    if not drop_gross and gross_str:
        pieces.append(gross_str)
    if not drop_net and net_str:
        pieces.append(net_str)
    tok_part = " / ".join(pieces)
    if tok_part:
        return f"[dim]{tok_part} · {cost_str}[/dim]"
    return f"[dim]{cost_str}[/dim]"


def _update_agents_state(
    agents: dict[str, dict[str, str]], event: dict
) -> None:
    """Mutate `agents` in place from a single inbound event.

    Tracks per-agent activity for the footer. Spawn / terminate use
    a regex against the existing info-event message format so the
    protocol shape doesn't have to change.
    """
    kind = event.get("type")
    agent_id = event.get("agent_id")
    key = agent_id or "root"

    if kind == "tool_call_started":
        agents.setdefault(key, {"status": "idle"})["status"] = (
            f"· {event.get('name', '?')}"
        )
        return
    if kind in ("tool_result", "assistant_text"):
        if key in agents:
            agents[key]["status"] = "thinking"
        return
    if kind == "ready":
        if key in agents:
            agents[key]["status"] = "ready"
        return
    if kind == "agent_error":
        if key in agents:
            agents[key]["status"] = "error"
        return
    if kind == "info":
        msg = event.get("message", "")
        m = _SPAWN_INFO_RE.search(msg)
        if m:
            agents[m.group("sid")] = {"status": "idle"}
            return
        m = _TERM_INFO_RE.search(msg)
        if m:
            agents.pop(m.group("sid"), None)
            return
    if kind == "checklist":
        # Always lands on root: the checklist is a per-session
        # construct, not per-agent. (Subagents don't get their own
        # list — see pyagent/checklist.py.) Compute the footer
        # summary here so _render_status doesn't have to re-walk
        # the task list on every redraw.
        tasks = event.get("tasks") or []
        slot = agents.setdefault("root", {"status": "thinking"})
        if not tasks:
            slot.pop("checklist", None)
            slot["checklist_tasks"] = []
            return
        total = sum(1 for t in tasks if t.get("status") != "cancelled")
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        current = next(
            (t for t in tasks if t.get("status") == "in_progress"), None
        ) or next(
            (t for t in tasks if t.get("status") == "pending"), None
        )
        slot["checklist"] = {
            "completed": completed,
            "total": total,
            "current_title": current.get("title", "") if current else "",
        }
        # Stash the full list so /tasks can render it without an IPC
        # round-trip. Cheap (≤ ~20 entries) and avoids an additional
        # request/response event type.
        slot["checklist_tasks"] = tasks
        return
    if kind == "notes_unread":
        # Root-only event from agent_proc (issue #65). Stash on root
        # so the footer (#67) can render `msgs:N` without polling.
        slot = agents.setdefault("root", {"status": "thinking"})
        slot["notes_unread"] = {
            "count": int(event.get("count", 0) or 0),
            "by_severity": dict(event.get("by_severity", {})),
        }
        return
    if kind == "usage":
        slot = agents.setdefault(key, {"status": "idle"})
        # Use .get(k, 0) + … so old two-key dicts in long-running state
        # (or session.jsonl re-replays predating the cache schema) don't
        # KeyError on cache_creation / cache_read.
        tokens = slot.setdefault(
            "tokens",
            {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        )
        for k in ("input", "output", "cache_creation", "cache_read"):
            tokens[k] = tokens.get(k, 0) + int(event.get(k, 0) or 0)
        return
    if kind == "context_status":
        # Root-emitted (subagents could too in principle, but the
        # warning that matters for footer real estate is the root's
        # context). Stash the latest reading; `_context_segment`
        # reads from here on every footer redraw.
        slot = agents.setdefault(key, {"status": "thinking"})
        slot["context"] = {
            "pct": int(event.get("pct", 0) or 0),
            "used": int(event.get("used", 0) or 0),
            "window": int(event.get("window", 0) or 0),
        }
        return


def _build_input_history(conversation: list[Any]) -> InMemoryHistory:
    """Seed an `InMemoryHistory` from prior user prompts so up/down at
    the prompt cycles through what was typed before — same per-
    process semantics readline gave us. A persistent on-disk history
    would be a behavior change (cross-session bleed-through);
    intentionally avoided here.

    History is built eagerly; the actual `PromptSession` is
    constructed lazily inside the REPL loop to avoid prompt_toolkit's
    "Input is not a terminal" warning firing in non-tty contexts
    (tests, pipes) that import this module without ever prompting.

    Filters to user messages with string content; tool-result turns
    are skipped because they don't have a `content` string.
    """
    history = InMemoryHistory()
    for entry in conversation:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        history.append_string(content)
    return history


def _resume_callback(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    if value != "__list__":
        return value
    ids = Session.list_ids()
    if not ids:
        click.echo("no sessions found")
    else:
        for sid in ids:
            click.echo(sid)
    ctx.exit()


_TASK_STATUS_GLYPH = {
    "pending": "○",
    "in_progress": "▶",
    "completed": "✓",
    "cancelled": "✗",
}


def _print_tasks(agents: dict) -> None:
    """Print the full checklist inline. Bound to the `/tasks` slash
    command. Reads the latest snapshot stashed by the `checklist`
    event; no IPC round-trip needed."""
    tasks = agents.get("root", {}).get("checklist_tasks") or []
    if not tasks:
        console.print("[dim]no tasks[/dim]")
        return
    for t in tasks:
        glyph = _TASK_STATUS_GLYPH.get(t.get("status", ""), "·")
        title = t.get("title", "")
        note = t.get("note", "")
        line = f"[dim]  {glyph} {title}[/dim]"
        if note:
            line += f"  [dim grey42]— {note}[/dim grey42]"
        console.print(line)


def _handle_model_command(
    parent_conn: Connection, line: str, current_model: str
) -> str:
    """Parse `/model <spec>` and (on success) ask the child to swap.

    `spec` is either a role name (resolved via `roles.resolve`) or a
    raw provider/model string. On bad input, prints an error and
    returns `current_model` unchanged. On success, sends a `set_model`
    event upstream and returns the resolved model string for the
    caller to adopt as its display value.

    The CLI updates its display optimistically — if the child fails
    to construct the new client (e.g. missing API key), it emits an
    `info` event with the error and leaves its existing client in
    place; the caller will see the message on the next turn.
    """
    parts = line.split(maxsplit=1)
    if len(parts) < 2:
        console.print(
            "[red]usage: /model <provider[/model-name]> | <role-name>[/red]"
        )
        return current_model
    spec = parts[1].strip()
    try:
        resolved, _ = roles.resolve(spec)
    except ValueError as e:
        console.print(f"[red]bad model spec {spec!r}: {e}[/red]")
        return current_model
    if not resolved:
        console.print("[red]empty model spec[/red]")
        return current_model
    try:
        protocol.send(parent_conn, "set_model", model=resolved)
    except (BrokenPipeError, OSError):
        console.print("[red]agent subprocess died[/red]")
        return current_model
    console.print(f"[dim]model: {resolved}[/dim]")
    return resolved


def _prompt_message(busy: bool) -> ANSI:
    """Build the prompt message — a thin horizontal divider above the
    `> ` input arrow when idle, just `> ` while a turn is in flight.

    The divider marks a turn boundary: it should only appear once
    the agent has finished responding and is genuinely waiting for
    the next input. Drawing it the moment the user hits Enter (i.e.
    *before* the agent has produced any output) is confusing — the
    user sees a fresh boundary line, then text streams in *above*
    it, making the divider feel out of order.

    Suppressing the divider while busy delays the boundary until
    `turn_complete` flips `turn_busy` back to False; the prompt then
    invalidates and the divider drops in cleanly above the arrow.

    Recomputed at every redraw so a terminal resize between turns
    picks up the new width without restart.
    """
    if busy:
        return ANSI("> ")
    width = shutil.get_terminal_size((80, 24)).columns
    divider = "─" * max(8, width - 1)
    # \x1b[2m = dim, \x1b[0m = reset
    return ANSI(f"\x1b[2m{divider}\x1b[0m\n> ")


_PERMS_HEAD_PREVIEW_MAX = 30


_CTX_WARN_PCT = 80
_CTX_DANGER_PCT = 95


def _context_segment(agents: dict) -> str:
    """Render the footer's context-utilization segment (' · ctx: NN%')
    or empty.

    Reads the most recent `context_status` reading stashed on the
    root agent by `_update_agents_state`. Hidden when no reading
    has arrived yet (first turn before usage flows) or when the
    model's window is unknown (stub clients, older Ollama). Color
    escalates yellow at 80%, red at 95% — matches the chat info
    thresholds emitted by `agent_proc._emit_context_status` so the
    footer signal and the chat warning are visually consistent.
    """
    slot = agents.get("root")
    if not isinstance(slot, dict):
        return ""
    ctx = slot.get("context")
    if not isinstance(ctx, dict):
        return ""
    window = int(ctx.get("window", 0) or 0)
    if window <= 0:
        return ""
    pct = int(ctx.get("pct", 0) or 0)
    if pct >= _CTX_DANGER_PCT:
        return f" · [red]ctx: {pct}%[/red]"
    if pct >= _CTX_WARN_PCT:
        return f" · [yellow]ctx: {pct}%[/yellow]"
    return f" · ctx: {pct}%"


def _perms_segment(
    perms: "collections.deque[dict]", drop_head: bool = False
) -> str:
    """Render the footer's permissions segment (' · perms: …') or empty.

    Issue #69 — when N>=1 concurrent permission_request events are in
    flight, show the count and a preview of the head target so the
    user knows what they're answering. Head target is truncated to
    ~30 chars so the footer stays one line on typical terminals.

      - 0 entries → empty (segment drops out)
      - 1 entry   → ` · perms: <target>` (or ` · perms: 1` if
        `drop_head` — the first step in the degradation pipeline)
      - N>1       → ` · perms: N (head: <target>)` or ` · perms: N`
        when `drop_head` is set.
    """
    n = len(perms)
    if n == 0:
        return ""
    if drop_head:
        return f" · perms: {n}"
    head_target = perms[0].get("target", "?")
    if len(head_target) > _PERMS_HEAD_PREVIEW_MAX:
        head_target = head_target[: _PERMS_HEAD_PREVIEW_MAX - 1] + "…"
    if n == 1:
        return f" · perms: {head_target}"
    return f" · perms: {n} (head: {head_target})"


# Braille spinner — 10 frames, indistinguishable in 0-width-glyph
# fonts but reads as a smooth rotating dot in any modern terminal.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_FPS = 10  # ticks per second; chosen to feel "alive" without
                   # being distracting. The bottom_toolbar's
                   # refresh_interval needs to be ≤ 1/_SPINNER_FPS to
                   # actually render every frame.


def _tree_busy(agents: dict) -> bool:
    """True iff any agent in the tree is non-`ready`/non-`error`.

    Broader than the old `turn_busy` — if root finished but a
    subagent is still working, the spinner should keep spinning.
    Truthful signal per issue #67's spinner predicate.
    """
    if not agents:
        return False
    for info in agents.values():
        status = info.get("status", "")
        if status not in ("ready", "error"):
            return True
    return False


def _spinner_segment(busy: bool) -> str:
    """ANSI-encoded spinner prefix when `busy`, empty string otherwise.

    Animation is driven by `time.monotonic()` rather than a frame
    counter so the spinner stays smooth across redraws even if
    prompt_toolkit's refresh interval drifts. Hidden when idle so
    the footer doesn't pretend the agent is doing work.
    """
    if not busy:
        return ""
    idx = int(time.monotonic() * _SPINNER_FPS) % len(_SPINNER_FRAMES)
    return f"\x1b[2m{_SPINNER_FRAMES[idx]}\x1b[0m "


# Right-zone budget: the spec caps it at 28 cols so wide terminals
# don't swallow huge stretches of footer with a stale dollar figure.
_RIGHT_ZONE_MAX = 28
# Tier-C agent-count threshold: lazygit-style heuristic. Past 6 live
# agents, even Tier B feels noisy on most terminals.
_TIER_C_AGENT_THRESHOLD = 6


def _visible_width(s: str) -> int:
    """Visible cell width of an ANSI- or rich-styled string.

    Strips ANSI escape sequences and rich `[tag]` markup before
    measuring. wcswidth handles double-width and zero-width glyphs
    (Braille spinner registers as 1 cell — wcswidth knows).
    """
    no_ansi = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)
    no_markup = re.sub(r"\[/?[a-zA-Z #]+\]", "", no_ansi)
    w = wcswidth(no_markup)
    if w < 0:
        return len(no_markup)
    return w


def _markup_to_ansi(markup: str, width: int) -> str:
    """Render a rich-markup string through a throwaway Console to ANSI.

    Width is fixed up-front so rich doesn't soft-wrap our pre-padded
    composition. force_terminal=True forces escape emission even
    when stdout isn't a tty (test harness, piped capture).
    """
    if not markup:
        return ""
    buf = io.StringIO()
    Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=max(width, 1),
    ).print(markup, end="")
    return buf.getvalue()


_SEVERITY_COLORS = {
    "info": "cyan",
    "warn": "yellow",
    "alert": "red",
}


def _style_perms(text: str) -> str:
    """Wrap a perms segment in bold-yellow markup — the brightest
    pixel in the bar when N>0 (issue #67). Bold is intentional here:
    perms is the one segment that warrants it, because it's literally
    blocking the agent."""
    return f"[bold yellow]{text}[/bold yellow]"


def _style_msgs(text: str, severity: str | None) -> str:
    """Severity-color the msgs segment, or dim if no severity."""
    color = _SEVERITY_COLORS.get(severity or "", "dim")
    return f"[{color}]{text}[/{color}]"


def _style_left(text: str, has_error: bool) -> str:
    """Wrap the left-zone center body. Errors paint red; everything
    else stays dim (the footer is ambient — see spec's visual rules).
    """
    if has_error:
        return f"[red]{text}[/red]"
    return f"[dim]{text}[/dim]"


def _has_error(agents: dict) -> bool:
    return any(
        a.get("status") == "error" for a in agents.values()
    )


def _compose_footer(
    agents: dict,
    model: str,
    perms: "collections.deque[dict]",
    cols: int,
) -> str:
    """Width-aware three-zone composition. Returns the ANSI-encoded
    bottom_toolbar line.

    Layout: `[spinner] LEFT  …pad…  RIGHT`. The right zone is the
    contract — it's pinned to the right edge and never truncated.
    The left zone degrades through a fixed priority order until the
    whole line fits in `cols` columns; if it still doesn't fit, the
    center (subagent labels / state) is truncated with `…`.
    """
    busy = _tree_busy(agents)
    spinner_ansi = _spinner_segment(busy)
    spinner_w = _visible_width(spinner_ansi)
    has_error = _has_error(agents)
    multi_agent = len(agents) > 1

    # Right zone composition (drop-tier knobs flipped during
    # degradation). Pre-render to ANSI once so width math sees the
    # same byte sequence we'll embed. Anything wider than 28 cols
    # forces an internal drop_gross / drop_net step so the right
    # zone always honors its budget.
    def render_right(drop_gross: bool, drop_net: bool) -> tuple[str, int]:
        for dg, dn in ((drop_gross, drop_net), (True, drop_net), (True, True)):
            markup = _format_right_zone_markup(
                agents, model, drop_gross=dg, drop_net=dn
            )
            if not markup:
                return "", 0
            ansi = _markup_to_ansi(markup, max(cols, _RIGHT_ZONE_MAX))
            w = _visible_width(ansi)
            if w <= _RIGHT_ZONE_MAX:
                return ansi, w
        # Fall through with the most-degraded variant even if it still
        # exceeds the cap (extreme edge case — 8-digit cost number).
        return ansi, w

    # Helper: compose left given a set of degradation choices.
    def build_left(
        agent_tier: str,
        drop_perms_head: bool,
        drop_msgs_severity: bool,
        drop_checklist_title: bool,
        drop_msgs: bool,
        drop_perms: bool,
    ) -> str:
        """Plain-text left zone (no rich markup) so we can measure
        width before deciding to recurse into a tighter tier."""
        if multi_agent:
            if agent_tier == "A":
                center = _agents_tier_a(agents)
            elif agent_tier == "B":
                center = _agents_tier_b(agents)
            else:
                center = _agents_tier_c(agents)
        else:
            center = _root_status_text(agents)
        center += _checklist_segment(agents, drop_title=drop_checklist_title)
        if not drop_perms:
            center += _perms_segment(perms, drop_head=drop_perms_head)
        if not drop_msgs:
            msgs_text, _ = _msgs_segment(agents, drop_severity_tag=drop_msgs_severity)
            center += msgs_text
        # Context utilization sits at the tail of the center zone:
        # less load-bearing than checklist/perms/msgs (those are
        # action items), but still worth a glance. No degradation
        # path — it's a single short atom that drops itself when the
        # window is unknown.
        center += _context_segment(agents)
        return center

    # Degradation pipeline. Each step makes one targeted concession
    # in the order the spec lists (1..9). We try the budget after
    # each and stop on the first fit.
    def fits(left_text: str, drop_gross: bool, drop_net: bool) -> tuple[bool, str, int]:
        right_a, right_wlocal = render_right(drop_gross, drop_net)
        left_w = _visible_width(left_text)
        # `+1` for the minimum single-space gap between left and right
        # when the right zone is non-empty.
        gap = 1 if right_a else 0
        return spinner_w + left_w + gap + right_wlocal <= cols, right_a, right_wlocal

    # The tuple structure is the dial set the composer has to pick:
    # (agent_tier, drop_perms_head, drop_msgs_severity,
    #  drop_checklist_title, drop_gross, drop_net, drop_msgs, drop_perms)
    # Order matches the spec's 1..9 priority list.
    steps: list[tuple[str, bool, bool, bool, bool, bool, bool, bool]] = []
    # Decide the starting agent tier. If there are >6 agents we go
    # straight to Tier C — the count alone already says enough.
    base_tier = "A"
    if multi_agent and len(agents) > _TIER_C_AGENT_THRESHOLD:
        base_tier = "C"

    # Step 0: nothing dropped.
    steps.append((base_tier, False, False, False, False, False, False, False))
    # Step 1: drop perms head preview.
    steps.append((base_tier, True, False, False, False, False, False, False))
    # Step 2: drop msgs severity tag.
    steps.append((base_tier, True, True, False, False, False, False, False))
    # Step 3: drop checklist title.
    steps.append((base_tier, True, True, True, False, False, False, False))
    # Step 4: drop gross.
    steps.append((base_tier, True, True, True, True, False, False, False))
    # Step 5: tier collapse A → B → C.
    if multi_agent and base_tier == "A":
        steps.append(("B", True, True, True, True, False, False, False))
        steps.append(("C", True, True, True, True, False, False, False))
    elif multi_agent and base_tier == "B":
        steps.append(("C", True, True, True, True, False, False, False))
    # Step 6: drop net.
    final_tier = "C" if multi_agent else base_tier
    steps.append((final_tier, True, True, True, True, True, False, False))
    # Step 7: drop msgs entirely.
    steps.append((final_tier, True, True, True, True, True, True, False))
    # Step 8: drop perms entirely (last resort — always-on signal).
    steps.append((final_tier, True, True, True, True, True, True, True))

    chosen_step: tuple[str, bool, bool, bool, bool, bool, bool, bool] | None = None
    chosen_right_a = ""
    chosen_right_w = 0
    chosen_left_text = ""
    truncated = False
    for tier, dph, dms, dct, dg, dn, dmsgs, dperms in steps:
        left_text = build_left(tier, dph, dms, dct, dmsgs, dperms)
        ok, right_a, right_wlocal = fits(left_text, dg, dn)
        if ok:
            chosen_step = (tier, dph, dms, dct, dg, dn, dmsgs, dperms)
            chosen_right_a = right_a
            chosen_right_w = right_wlocal
            chosen_left_text = left_text
            break

    if chosen_step is None:
        # Even the most-degraded variant didn't fit. Truncate the
        # center with `…` so the right zone keeps its column.
        tier, dph, dms, dct, dg, dn, dmsgs, dperms = steps[-1]
        left_text = build_left(tier, dph, dms, dct, dmsgs, dperms)
        right_a, right_wlocal = render_right(dg, dn)
        gap = 1 if right_a else 0
        max_left = max(0, cols - spinner_w - gap - right_wlocal)
        if _visible_width(left_text) > max_left and max_left > 1:
            left_text = left_text[: max_left - 1] + "…"
            truncated = True
        chosen_step = (tier, dph, dms, dct, dg, dn, dmsgs, dperms)
        chosen_right_a = right_a
        chosen_right_w = right_wlocal
        chosen_left_text = left_text

    tier, dph, dms, dct, dg, dn, dmsgs, dperms = chosen_step
    right_a = chosen_right_a
    right_wlocal = chosen_right_w

    if truncated:
        # Single-style the truncated text — no per-segment coloring
        # because the segment boundaries are gone.
        left_markup = _style_left(chosen_left_text, has_error)
    else:
        if multi_agent:
            if tier == "A":
                center = _agents_tier_a(agents)
            elif tier == "B":
                center = _agents_tier_b(agents)
            else:
                center = _agents_tier_c(agents)
        else:
            center = _root_status_text(agents)
        center += _checklist_segment(agents, drop_title=dct)
        center_markup = _style_left(center, has_error)
        perms_text = "" if dperms else _perms_segment(perms, drop_head=dph)
        perms_markup = _style_perms(perms_text) if perms_text else ""
        if dmsgs:
            msgs_markup = ""
        else:
            msgs_text, sev = _msgs_segment(agents, drop_severity_tag=dms)
            msgs_markup = _style_msgs(msgs_text, sev) if msgs_text else ""
        # Context segment carries its own (yellow / red) styling at
        # threshold so we don't pipe it through `_style_left`.
        ctx_markup = _context_segment(agents)
        left_markup = f"{center_markup}{perms_markup}{msgs_markup}{ctx_markup}"

    left_ansi = _markup_to_ansi(left_markup, max(cols, 1))
    left_w = _visible_width(left_ansi)
    pad = max(1, cols - spinner_w - left_w - right_wlocal) if right_a else 0
    return spinner_ansi + left_ansi + (" " * pad) + right_a


def _render_status_ansi(
    agents: dict,
    model: str,
    perms: "collections.deque[dict]",
    cols: int | None = None,
) -> str:
    """Render the bottom_toolbar content as ANSI-encoded bytes.

    Three-zone layout per issue #67: spinner+left, filler, right
    (`gross / net · $cost`). Width-aware degradation is in
    `_compose_footer`; this is just the entry point that reads the
    terminal width when no explicit `cols` is supplied.
    """
    if cols is None:
        cols = shutil.get_terminal_size((120, 24)).columns
    return _compose_footer(agents, model, perms, cols)


def _print_event(event: dict) -> None:
    """Render a single inbound event to the console.

    Splits per-event-type handling out of the old `_drive_turn` so
    the asyncio reader (which is invoked by `loop.add_reader` on the
    pipe fd) can dispatch one event per call. Only handles things
    that produce visible output — state mutations (agents_state,
    queue draining, etc.) live in the caller.
    """
    kind = event.get("type")
    agent_id = event.get("agent_id")
    if kind == "assistant_text_delta":
        _on_text_delta(event["text"], agent_id=agent_id)
        return
    if kind == "assistant_text":
        key = agent_id or "root"
        state = _streaming_state.pop(key, None)
        if state is None:
            # Non-streaming provider — full markdown render as before.
            _on_text(event["text"], agent_id=agent_id)
        else:
            # Provider streamed — wipe the dim plain text we just
            # printed, then call _on_text so the same text re-renders
            # with full markdown formatting (bold, headers, lists,
            # code blocks). Cursor-prev-line + clear-to-end works on
            # any ANSI-aware terminal; we only emit it when the
            # console is a real terminal so piped output (test
            # captures, log redirects) doesn't get garbled by
            # control sequences.
            advance = _count_cursor_advance(
                state["buffer"], state["width"]
            ) + state["header_advances"]
            if advance > 0 and console.is_terminal:
                import sys
                # `\x1b[<n>F` = Cursor Previous Line: up n lines, col 1.
                # `\x1b[J`   = Erase from cursor to end of screen.
                sys.stdout.write(f"\x1b[{advance}F\x1b[J")
                sys.stdout.flush()
            _on_text(event["text"], agent_id=agent_id)
    elif kind == "tool_call_started":
        _on_tool_call(event["name"], event["args"], agent_id=agent_id)
    elif kind == "tool_result":
        _on_tool_result(event["name"], event["content"], agent_id=agent_id)
    elif kind == "info":
        label = _agent_label(agent_id)
        console.print(f"{label}[dim]{event['message']}[/dim]")
    elif kind == "ready":
        label = _agent_label(agent_id)
        console.print(f"{label}[dim]ready[/dim]")
    elif kind == "subagent_ask":
        # A subagent is asking its parent a question mid-turn.
        # Yellow so the user spots cross-agent conversation in
        # the same scan they use for permission prompts. The
        # parent will see the same text as a synthesized user
        # message at the start of its next turn (issue #47).
        label = _agent_label(agent_id)
        req_id = event.get("request_id", "")
        question = event.get("question", "") or ""
        console.print(
            f"{label}[yellow]asks parent (req={req_id}):[/yellow] "
            f"[dim]{question}[/dim]"
        )
    elif kind == "subagent_note":
        # A subagent dropped a non-blocking note to its parent
        # (issue #64). The parent's IO thread also queued it onto
        # the parent's pending_async_replies; the model sees it
        # at its next LLM call. Surface in the transcript so the
        # human can read along.
        label = _agent_label(agent_id)
        severity = event.get("severity", "info") or "info"
        text = event.get("text", "") or ""
        # Color severity: warn / alert get yellow to draw the eye;
        # info stays dim.
        sev_style = "yellow" if severity in ("warn", "alert") else "cyan"
        console.print(
            f"{label}[{sev_style}]notes ({severity}):[/{sev_style}] "
            f"[dim]{text}[/dim]"
        )
    elif kind == "agent_error":
        if agent_id is not None:
            console.print(
                f"{_agent_label(agent_id)}[red]Error:[/red] "
                f"{event['kind']}: {event['message']}"
            )
        elif event.get("kind") == "KeyboardInterrupt":
            console.print("[dim]interrupted[/dim]")
        else:
            console.print(
                f"[red]Error:[/red] {event['kind']}: {event['message']}"
            )


def _handle_perms_command(
    line: str, perms: "collections.deque[dict]"
) -> None:
    """Implement /perms (list) and /perms <n> (jump-the-queue).

    Issue #69. With multiple concurrent permission requests, the user
    needs to see what's pending and answer out of order if useful
    (e.g. dismiss a less-important one first). `/perms <n>` rotates
    entry index `n` (1-based) to the head so the next y/n/a answers
    that one.
    """
    parts = line.split()
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "":
        if not perms:
            console.print("[dim]no pending permission requests[/dim]")
            return
        for i, entry in enumerate(perms, 1):
            target = entry.get("target", "?")
            sid = entry.get("agent_id") or "root"
            tag = "" if i > 1 else " [dim](active)[/dim]"
            console.print(
                f"[dim]  {i}. {sid}: {target}[/dim]{tag}"
            )
        return
    try:
        idx = int(sub)
    except ValueError:
        console.print(
            f"[red]unknown perms command {sub!r}; "
            f"use /perms or /perms <n>[/red]"
        )
        return
    if not perms:
        console.print("[dim]no pending permission requests[/dim]")
        return
    if idx < 1 or idx > len(perms):
        console.print(
            f"[red]/perms {idx}: out of range (1..{len(perms)})[/red]"
        )
        return
    if idx == 1:
        console.print("[dim]already active[/dim]")
        return
    # Move entry at position idx-1 to the head. Rotating preserves
    # arrival order of the others, which keeps the list intuitive.
    entry = perms[idx - 1]
    del perms[idx - 1]
    perms.appendleft(entry)
    target = entry.get("target", "?")
    console.print(
        f"[dim]active: {target} (was index {idx})[/dim]"
    )


async def _repl_async(
    parent_conn: Connection,
    model: str,
    agents_state: dict[str, dict[str, str]],
    input_history: InMemoryHistory,
) -> str:
    """Async REPL: persistent input field, queued typing during turns,
    cancel via Esc, footer redrawn live in prompt_toolkit's bottom
    toolbar. Returns one of: "eof", "fatal", "interrupt".

    The previous synchronous `_drive_turn` polling loop is replaced
    with `loop.add_reader` on the pipe's fileno — every inbound event
    triggers `on_pipe` exactly once, no busy-wait. Slash commands and
    submit handling run on the same asyncio loop as the prompt, so
    there's no thread synchronization to reason about.

    State machine (issue #68 / #69):
      - perms non-empty → next typed line is a y/n/a answer to the
        head request; routed back over the pipe with the head's
        request_id.
      - turn_busy → next typed line is sent as a `user_note` event;
        the agent surfaces it as `[user adds]: ...` mid-turn.
      - idle → next typed line is sent as `user_prompt` (existing
        path).
    """
    # Pending permission requests, FIFO. Each entry:
    # {target, agent_id, request_id}. /perms lists; /perms <n>
    # rotates index n to head; submit-while-non-empty answers head.
    perms: collections.deque[dict] = collections.deque()
    state: dict[str, Any] = {
        "model": model,
        "turn_busy": False,
        "fatal": False,
        "interrupted": False,
    }

    loop = asyncio.get_running_loop()

    def send_or_die(event_type: str, **payload: Any) -> bool:
        try:
            protocol.send(parent_conn, event_type, **payload)
            return True
        except (BrokenPipeError, OSError):
            console.print("[red]agent subprocess died[/red]")
            state["fatal"] = True
            pt_session.app.exit(result="")
            return False

    def on_pipe() -> None:
        """asyncio reader callback for `parent_conn`.

        Drains every event currently buffered (the OS-level pipe
        readiness fires once even if multiple events arrived; calling
        recv() inside `if poll(0)` keeps us from blocking on a slow
        sender).
        """
        while True:
            if not parent_conn.poll(0):
                break
            try:
                event = parent_conn.recv()
            except (EOFError, OSError):
                state["fatal"] = True
                pt_session.app.exit(result="")
                return
            _update_agents_state(agents_state, event)
            kind = event.get("type")
            agent_id = event.get("agent_id")
            if kind == "permission_request":
                # Issue #69: append to the deque (don't overwrite a
                # single slot). The head is the active prompt the
                # next y/n/a answers; /perms <n> can reorder.
                perms.append({
                    "target": event["target"],
                    "agent_id": agent_id,
                    "request_id": event.get("request_id", ""),
                })
                # Only print the inline banner for the new arrival;
                # the head's status sits on the footer continuously.
                tail_note = (
                    "" if len(perms) == 1
                    else f" [dim](queued; {len(perms)} pending)[/dim]"
                )
                console.print(
                    f"\n{_agent_label(agent_id)}"
                    f"[yellow]access requested OUTSIDE workspace:[/yellow]"
                    f"{tail_note}\n"
                    f"  workspace: {permissions.workspace()}\n"
                    f"  target:    {event['target']}\n"
                    f"[yellow]answer at the prompt: y / n / a[/yellow]"
                )
            elif kind == "turn_complete" and agent_id is None:
                state["turn_busy"] = False
                # Issue #68: no local input queue to drain anymore.
                # If the user typed during the turn, those lines
                # already landed as `user_note` events; the agent
                # handled them mid-turn (or promoted them to a
                # fresh prompt if the idle-window race fired).
                agents_state.setdefault(
                    "root", {"status": "ready"}
                )["status"] = "ready"
            elif kind == "agent_error":
                _print_event(event)
                if agent_id is None:
                    if event.get("fatal"):
                        state["fatal"] = True
                        pt_session.app.exit(result="")
                        return
                    # Non-fatal root error: turn is over. Surface
                    # the error and let the user decide whether to
                    # keep going.
                    state["turn_busy"] = False
            elif kind in ("usage", "checklist", "notes_unread"):
                # State already updated; no inline render. Footer
                # picks it up on the next bottom_toolbar refresh.
                pass
            else:
                _print_event(event)
            # Trigger a footer redraw.
            pt_session.app.invalidate()

    def bottom_toolbar() -> ANSI:
        # Three-zone composition lives in `_compose_footer` now; the
        # spinner predicate broadened to cover non-root activity (see
        # `_tree_busy`) so a finished root with a still-thinking
        # subagent keeps the heartbeat visible.
        cols = shutil.get_terminal_size((120, 24)).columns
        return ANSI(
            _render_status_ansi(
                agents_state,
                state["model"],
                perms,
                cols=cols,
            )
        )

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _esc(event: Any) -> None:
        # Esc means "cancel the in-flight turn" when busy. The agent
        # propagates cancel down to all subagents and SIGKILLs in-flight
        # shells. Pending permission requests get cleared locally too —
        # the agent's tearing down whatever was waiting on them. When
        # idle, no-op (don't interfere with line editing).
        if not state["turn_busy"]:
            return
        send_or_die("cancel")
        perms.clear()
        pt_session.app.invalidate()

    # prompt_toolkit's default `class:bottom-toolbar` style is
    # `reverse`, which produces a bright bar that fights the dim
    # ANSI colors emitted by `_render_status_ansi`. Use a near-black
    # gray instead — just enough lift off the terminal background to
    # register as a separate band, dim enough that the rich-emitted
    # text remains the dominant ink.
    pt_style = Style.from_dict({
        "bottom-toolbar": "noreverse bg:#1c1c1c fg:default",
        "bottom-toolbar.text": "noreverse bg:#1c1c1c fg:default",
    })

    pt_session: PromptSession = PromptSession(
        history=input_history,
        bottom_toolbar=bottom_toolbar,
        # 0.1s tick so the spinner runs at its full 10 fps; lower
        # would burn CPU on idle redraws, higher would make the
        # spinner look choppy.
        refresh_interval=0.1,
        key_bindings=bindings,
        style=pt_style,
    )

    loop.add_reader(parent_conn.fileno(), on_pipe)
    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    # Pass the message as a callable so prompt_toolkit
                    # re-evaluates it on every redraw — the divider
                    # only renders once `turn_busy` flips back to
                    # False, so it can't appear above incoming output.
                    line = await pt_session.prompt_async(
                        lambda: _prompt_message(state["turn_busy"])
                    )
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / Ctrl-C at the prompt — clean exit.
                console.print()
                return "eof"
            if state["fatal"]:
                return "fatal"
            stripped = (line or "").strip()
            if not stripped:
                continue
            # Slash commands always process locally — they never go
            # through the perm_pending / busy / idle gate.
            if stripped.startswith("/perms"):
                _handle_perms_command(stripped, perms)
                continue
            if stripped.startswith("/model"):
                state["model"] = _handle_model_command(
                    parent_conn, stripped, state["model"]
                )
                continue
            if stripped == "/tasks":
                _print_tasks(agents_state)
                continue
            # State machine (issue #68 / #69):
            #   1. perms non-empty → answer head as y/n/a.
            #   2. turn_busy       → send user_note (mid-turn inject).
            #   3. idle            → send user_prompt (start new turn).
            if perms:
                answer = stripped.lower()
                if answer in ("y", "yes", "n", "no", "a", "always"):
                    decision = answer in ("y", "yes", "a", "always")
                    always = answer in ("a", "always")
                    head = perms.popleft()
                    target = head.get("target", "")
                    pending_agent_id = head.get("agent_id")
                    request_id = head.get("request_id", "")
                    if always:
                        permissions.pre_approve(target)
                    if not send_or_die(
                        "permission_response",
                        decision=decision,
                        always=always,
                        agent_id=pending_agent_id,
                        request_id=request_id,
                    ):
                        return "fatal"
                    # If more requests remain, surface the next head
                    # so the user knows what they're answering next.
                    if perms:
                        next_target = perms[0].get("target", "?")
                        console.print(
                            f"[dim]next perm: {next_target} "
                            f"({len(perms)} pending)[/dim]"
                        )
                else:
                    console.print(
                        f"[red]unrecognized: {answer!r} — please answer "
                        f"y, n, or a (or /perms to list, /perms <n> "
                        f"to reorder)[/red]"
                    )
                continue
            if state["turn_busy"]:
                # Mid-turn typed input: ship as user_note. Agent
                # surfaces it as `[user adds]: …` at next LLM call.
                if not send_or_die("user_note", text=line):
                    return "fatal"
                preview = line if len(line) <= 60 else line[:57] + "..."
                console.print(
                    f"[dim grey42]>> note sent: {preview}[/dim grey42]"
                )
            else:
                if not send_or_die("user_prompt", prompt=line):
                    return "fatal"
                agents_state.setdefault(
                    "root", {"status": "thinking"}
                )["status"] = "thinking"
                state["turn_busy"] = True
    finally:
        try:
            loop.remove_reader(parent_conn.fileno())
        except (ValueError, OSError):
            pass


@click.command(
    epilog=(
        "API keys are read from environment variables:\n"
        "\n"
        "\b\n"
        "  anthropic   ANTHROPIC_API_KEY\n"
        "  openai      OPENAI_API_KEY\n"
        "  gemini      GEMINI_API_KEY (or GOOGLE_API_KEY)\n"
        "\n"
        "Companion scripts:\n"
        "\n"
        "\b\n"
        "  pyagent-skills    install/uninstall/list bundled skills\n"
        "  pyagent-sessions  list/delete/prune saved sessions"
    ),
)
@click.option(
    "--soul",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the SOUL.md path (default: ./SOUL.md if present, else <config-dir>/SOUL.md).",
)
@click.option(
    "--tools",
    "tools_md",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the TOOLS.md path (default: ./TOOLS.md if present, else <config-dir>/TOOLS.md).",
)
@click.option(
    "--primer",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Override the PRIMER.md path (default: ./PRIMER.md if present, else <config-dir>/PRIMER.md).",
)
@click.option(
    "--model",
    default=None,
    help=(
        "Provider, optionally with '/model-name' (e.g. anthropic, "
        "openai/gpt-4o). If unset: use config.default_model, else "
        "auto-detect from API-key env vars."
    ),
)
@click.option(
    "--list-models",
    "list_models_flag",
    is_flag=True,
    help=(
        "Print every model each provider advertises and exit. "
        "Built-ins return a hardcoded canonical list (no API key "
        "needed); plugin providers like ollama query their backend "
        "live. One unreachable backend renders as "
        "`(unavailable: ...)` and never blocks the rest."
    ),
)
@click.option(
    "--resume",
    "resume_id",
    is_flag=False,
    flag_value="__list__",
    default=None,
    metavar="SESSION_ID",
    is_eager=True,
    callback=_resume_callback,
    help="Resume an existing session by id (under .pyagent/sessions/). "
    "Pass with no value to list available sessions and exit.",
)
@click.option(
    "--reset-soul",
    is_flag=True,
    help="Overwrite <config-dir>/SOUL.md with the bundled default.",
)
@click.option(
    "--reset-tools",
    "reset_tools",
    is_flag=True,
    help="Overwrite <config-dir>/TOOLS.md with the bundled default.",
)
@click.option(
    "--reset-primer",
    is_flag=True,
    help="Overwrite <config-dir>/PRIMER.md with the bundled default.",
)
@click.option(
    "--reset-skills",
    is_flag=True,
    help="Remove every user-installed skill under <config-dir>/skills/. "
    "Bundled skills are unaffected (they ship with the package).",
)
@click.option(
    "--reset-all",
    is_flag=True,
    help="Shortcut: every --reset-* flag together (SOUL, TOOLS, PRIMER, skills). "
    "USER.md and MEMORY.md are owned by the memory-markdown plugin now; "
    "use `pyagent-plugins reset memory-markdown` to wipe them.",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt for destructive resets (skills).",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show INFO-level logs from pyagent.",
)
def main(
    soul: Path | None,
    tools_md: Path | None,
    primer: Path | None,
    model: str | None,
    list_models_flag: bool,
    resume_id: str | None,
    reset_soul: bool,
    reset_tools: bool,
    reset_primer: bool,
    reset_skills: bool,
    reset_all: bool,
    assume_yes: bool,
    verbose: bool,
) -> None:
    install_traceback(show_locals=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if verbose:
        logging.getLogger("pyagent").setLevel(logging.INFO)

    if list_models_flag:
        # Plugins must be loaded so plugin-registered providers (like
        # ollama) show up in the listing. plugins.load() only runs
        # register() — no session-start hooks fire — so this is cheap
        # and side-effect-free for the CLI exit path.
        from pyagent import plugins as _plugins

        _plugins.load()
        console.print(_render_models_listing())
        return

    will_reset_soul = reset_soul or reset_all
    will_reset_tools = reset_tools or reset_all
    will_reset_primer = reset_primer or reset_all
    will_reset_skills = reset_skills or reset_all
    any_reset = any(
        (
            will_reset_soul,
            will_reset_tools,
            will_reset_primer,
            will_reset_skills,
        )
    )

    if any_reset:
        skills_root = paths.config_dir() / "skills"
        skill_dirs: list[Path] = []
        if will_reset_skills and skills_root.exists():
            skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())

        destructive: list[str] = []
        if will_reset_skills:
            if skill_dirs:
                names = ", ".join(p.name for p in skill_dirs)
                destructive.append(
                    f"{len(skill_dirs)} user-installed skill(s): {names}"
                )
            else:
                destructive.append("user-installed skills (none currently)")

        if destructive and not assume_yes:
            console.print("[red]This will permanently remove:[/red]")
            for line in destructive:
                console.print(f"  - {line}")
            if not click.confirm("Continue?", default=False):
                console.print("[dim]aborted.[/dim]")
                return

        for flag, name, seed in (
            (will_reset_soul, "SOUL.md", "SOUL.md"),
            (will_reset_tools, "TOOLS.md", "TOOLS.md"),
            (will_reset_primer, "PRIMER.md", "PRIMER.md"),
        ):
            if flag:
                path = paths.reset_to_default(name, seed)
                console.print(f"[yellow]reset {name} → {path}[/yellow]")

        if will_reset_skills:
            for d in skill_dirs:
                shutil.rmtree(d)
                console.print(f"[yellow]removed user skill {d.name}[/yellow]")
            if not skill_dirs:
                console.print(f"[dim]no user-installed skills in {skills_root}[/dim]")

        if resume_id:
            console.print(
                f"[dim](--resume {resume_id} ignored: reset flags exit "
                "without launching the REPL)[/dim]"
            )
        return

    model = _resolve_model(model)

    cfg = config.load()
    cap_mb = int(cfg.get("session", {}).get("attachment_dir_cap_mb", 25))

    if resume_id:
        session = Session(session_id=resume_id, attachment_dir_cap_mb=cap_mb)
        if not session.exists():
            raise click.UsageError(f"session {resume_id!r} not found at {session.dir}")
    else:
        session = Session(attachment_dir_cap_mb=cap_mb)

    soul = paths.resolve("SOUL.md", override=soul, seed="SOUL.md")
    tools_md = paths.resolve("TOOLS.md", override=tools_md, seed="TOOLS.md")
    primer = paths.resolve("PRIMER.md", override=primer, seed="PRIMER.md")
    permissions.pre_approve(paths.config_dir())

    # CLI keeps a read-only view of history to seed prompt_toolkit's
    # in-memory up-arrow history and to render the "resumed N entries"
    # line; the child owns writes during the run.
    prior = session.load_history()
    input_history = _build_input_history(prior)

    agent_config = {
        "cwd": str(Path.cwd().resolve()),
        "model": model,
        "session_id": session.id,
        "soul_path": str(soul),
        "tools_path": str(tools_md),
        "primer_path": str(primer),
        "approved_paths": [str(p) for p in permissions.approved_paths()],
        "attachment_dir_cap_mb": cap_mb,
    }

    # spawn (not fork): pickles fresh, doesn't drag the parent's
    # threading state across, and behaves predictably under termios
    # mode changes from the cancel watcher.
    #
    # daemon=False (Phase 3): the agent process must be allowed to
    # spawn its own multiprocessing children (subagents). We trade
    # the auto-cleanup-on-parent-exit that daemon=True gave us for
    # the explicit try/finally below; the child also installs
    # PR_SET_PDEATHSIG as a belt-and-suspenders against the CLI
    # being SIGKILLed before the finally block runs.
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(
        target=agent_proc.child_main,
        args=(agent_config, child_conn),
        name="pyagent-agent",
        daemon=False,
    )
    proc.start()

    interrupted = False
    # Hoisted so the exit summary can read totals even if we bail
    # before the main loop populates this (e.g. during the ready
    # handshake).
    agents_state: dict[str, dict[str, str]] = {}
    try:
        # Parent doesn't need the child's end of the pipe; closing it
        # lets the parent's recv() see EOF promptly when the child dies.
        child_conn.close()

        console.print(f"[dim]session: {session.id}[/dim]")
        console.print(f"[dim]model:   {model}[/dim]")
        if prior:
            console.print(f"[dim]resumed {len(prior)} entries[/dim]")

        # Wait for the child's `ready` (or fatal error) before accepting input.
        while True:
            try:
                event = parent_conn.recv()
            except (EOFError, OSError):
                console.print("[red]agent subprocess exited before ready[/red]")
                return
            kind = event.get("type")
            if kind == "ready":
                break
            if kind == "info":
                console.print(f"[dim]{event['message']}[/dim]")
                continue
            if kind == "agent_error":
                console.print(
                    f"[red]agent failed to start:[/red] "
                    f"{event['kind']}: {event['message']}"
                )
                proc.join(timeout=2)
                return
            logger.warning("cli: unexpected pre-ready event %r", kind)

        logger.info("soul=%s tools=%s primer=%s", soul, tools_md, primer)
        # Per-agent state shared across turns. Root starts in `ready`
        # (the agent has bootstrapped and is waiting for input); the
        # submit branch in `_repl_async` flips it to `thinking` when
        # the user actually fires a turn. Subagents are added/removed
        # as info events flow through `_update_agents_state`.
        agents_state["root"] = {"status": "ready"}

        # All REPL loop state lives in `_repl_async`. Returns one of
        # "eof" (clean Ctrl-D / Ctrl-C at the prompt), "fatal"
        # (agent subprocess died mid-session), or "interrupt" (KI
        # arrived outside the prompt). asyncio.run owns its own
        # signal handling for SIGINT — Ctrl-C delivered to the CLI
        # process raises KeyboardInterrupt out of `prompt_async`,
        # caught inside the coroutine.
        outcome = asyncio.run(
            _repl_async(
                parent_conn,
                model,
                agents_state,
                input_history,
            )
        )
        if outcome == "fatal":
            console.print("[red]agent subprocess exited unexpectedly[/red]")
    except KeyboardInterrupt:
        # User Ctrl+C'd somewhere outside the input prompt's own
        # except (e.g. mid-turn while a tool was running, or during
        # the ready handshake). The cleanup below sends cancel + then
        # shutdown so the child has a chance to wind down its in-
        # flight turn instead of being blocked waiting for the next
        # event when shutdown finally arrives.
        interrupted = True
        console.print()
        console.print("[dim]interrupted[/dim]")
    finally:
        if proc.is_alive():
            if interrupted:
                # Cancel first so the child stops any in-flight tool
                # batch, then shutdown to break out of its main loop.
                try:
                    protocol.send(parent_conn, "cancel")
                except (BrokenPipeError, OSError):
                    pass
            try:
                protocol.send(parent_conn, "shutdown")
            except (BrokenPipeError, OSError):
                pass
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
        try:
            parent_conn.close()
        except Exception:
            pass

    if session.exists():
        console.print(f"[dim]to resume: pyagent --resume {session.id}[/dim]")
        in_tot, out_tot, cw_tot, cr_tot = _agents_tokens(agents_state)
        usage_suffix = _format_usage_suffix(
            in_tot, out_tot, model, cw_tot, cr_tot
        )
        if usage_suffix:
            console.print(f"[dim]usage:{usage_suffix}[/dim]")


if __name__ == "__main__":
    main()
