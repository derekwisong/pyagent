import asyncio
import collections
import io
import logging
import multiprocessing
import re
import shutil
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
    format_usage_suffix as _format_usage_suffix,
    is_anthropic_model as _is_anthropic_model,
    model_name as _model_name,
)


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


def _checklist_segment(agents: dict) -> str:
    """Return the footer's checklist segment ('· N/M · "title"') or empty.

    Reads the most recent checklist snapshot stashed under
    `agents["root"]["checklist"]` by `_update_agents_state`. Drops out
    cleanly when there's no list, or when every task is done — the
    point of the segment is *progress on something live*, not a
    monument to past work.
    """
    cl = agents.get("root", {}).get("checklist")
    if not cl:
        return ""
    total = cl.get("total", 0)
    completed = cl.get("completed", 0)
    if total <= 0 or completed >= total:
        return ""
    title = cl.get("current_title", "") or ""
    if len(title) > _CHECKLIST_TITLE_MAX:
        title = title[: _CHECKLIST_TITLE_MAX - 1] + "…"
    if title:
        return f" · {completed}/{total} · {title}"
    return f" · {completed}/{total}"


def _render_status(agents: dict, model: str = "") -> str:
    """Return the rich-markup string for the status footer.

    Single-agent (only root) → the classic `thinking…` text so the UI
    is unchanged for users not using subagents, plus a token/cost
    suffix once any LLM calls have happened.

    Multi-agent → `agent(status) │ agent(status) │ …` separated by box-
    drawing pipes. Order is insertion order (root first, then
    subagents in spawn order) which gives a stable left-to-right read.
    Token/cost suffix is the aggregate across the whole tree.

    Both renderings get a checklist segment appended when the root
    agent has a non-empty, not-yet-finished task list.
    """
    in_tot, out_tot, cw_tot, cr_tot = _agents_tokens(agents)
    suffix = _format_usage_suffix(in_tot, out_tot, model, cw_tot, cr_tot)
    checklist = _checklist_segment(agents)
    if len(agents) <= 1:
        status = agents.get("root", {}).get("status", "thinking")
        # `…` indicates active work — drop it for terminal states
        # (`ready`, `error`) so the always-on bottom_toolbar doesn't
        # lie about what the agent is doing while it sits idle
        # waiting for the next user input.
        trailing = "" if status in ("ready", "error") else "…"
        return f"[dim]{status}{trailing}{checklist}{suffix}[/dim]"
    parts = []
    for key, info in agents.items():
        label = "root" if key == "root" else key
        parts.append(f"{label}([cyan]{info['status']}[/cyan])")
    body = " [/dim][dim]│[/dim] [dim]".join(parts)
    return f"[dim]{body}{checklist}{suffix}[/dim]"


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


def _prompt_message() -> ANSI:
    """Build the multi-line prompt message: a thin horizontal divider
    line above the `> ` input arrow.

    Visually separates each turn so the input region is unmistakable
    in tmux/terminal scrollback. Unlike a fixed-bottom TUI (which
    would defeat tmux's `Ctrl-b PgUp`), this approach keeps the
    append-only output model — the divider becomes a per-turn
    bracket in scrolled history, marking where each prompt landed.

    Recomputed at every prompt iteration so a terminal resize
    between turns picks up the new width without restart.
    """
    width = shutil.get_terminal_size((80, 24)).columns
    divider = "─" * max(8, width - 1)
    # \x1b[2m = dim, \x1b[0m = reset
    return ANSI(f"\x1b[2m{divider}\x1b[0m\n> ")


_QUEUE_PREVIEW_MAX = 30


def _queue_segment(queue: "collections.deque[str]") -> str:
    """Render the footer's queue segment (' · queued: …') or empty.

    Mirrors the design from issue #42:
      - 0 entries → empty (segment drops out)
      - 1 entry  → ` · queued: "<head>"`
      - N>1      → ` · queued: N (next: "<head>")`
    Head is truncated to ~30 chars so the footer stays one line on
    typical terminals.
    """
    n = len(queue)
    if n == 0:
        return ""
    head = queue[0]
    if len(head) > _QUEUE_PREVIEW_MAX:
        head = head[: _QUEUE_PREVIEW_MAX - 1] + "…"
    if n == 1:
        return f' · queued: "{head}"'
    return f' · queued: {n} (next: "{head}")'


def _render_status_ansi(
    agents: dict,
    model: str,
    queue: "collections.deque[str]",
    perm_pending: str | None,
) -> str:
    """Render the bottom_toolbar content as ANSI-encoded bytes.

    prompt_toolkit accepts an `ANSI("...")` formatted text — we
    re-render rich markup through a non-attached Console so the
    existing `_render_status` styling carries over without duplication
    and a separate styling vocabulary. Queue depth and a pending
    permission notice get appended to the same line.
    """
    base = _render_status(agents, model)
    queued = _queue_segment(queue)
    perm = (
        f" · awaiting permission ({perm_pending}) — type y/n/a"
        if perm_pending
        else ""
    )
    # rich-markup → ANSI: render through a throwaway Console with
    # force_terminal so styles emit even when stdout isn't a tty.
    buf = io.StringIO()
    Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=shutil.get_terminal_size((120, 24)).columns,
    ).print(base + queued + perm, end="")
    return buf.getvalue()


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
    if kind == "assistant_text":
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


def _handle_queue_command(
    line: str, queue: "collections.deque[str]"
) -> None:
    """Implement /queue, /queue clear, /queue pop.

    The queue is the user's typed-but-undelivered input — entries
    accumulated while the agent was busy that haven't been pulled
    off as the next user_prompt yet.
    """
    parts = line.split()
    sub = parts[1] if len(parts) > 1 else ""
    if sub == "":
        if not queue:
            console.print("[dim]queue empty[/dim]")
            return
        for i, entry in enumerate(queue, 1):
            preview = entry if len(entry) <= 80 else entry[:77] + "..."
            console.print(f"[dim]  {i}. {preview}[/dim]")
        return
    if sub == "clear":
        n = len(queue)
        queue.clear()
        console.print(f"[dim]cleared {n} queued entries[/dim]")
        return
    if sub == "pop":
        if not queue:
            console.print("[dim]queue empty[/dim]")
            return
        dropped = queue.pop()
        preview = dropped if len(dropped) <= 60 else dropped[:57] + "..."
        console.print(f"[dim]popped: {preview!r}[/dim]")
        return
    console.print(
        f"[red]unknown queue command {sub!r}; "
        f"use /queue, /queue clear, /queue pop[/red]"
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
    queue draining run on the same asyncio loop as the prompt, so
    there's no thread synchronization to reason about.
    """
    queue: collections.deque[str] = collections.deque()
    state: dict[str, Any] = {
        "model": model,
        "turn_busy": False,
        # When set, the next submitted line is interpreted as a
        # y/n/a answer to a pending permission_request rather than
        # a user_prompt. Value is the pending request's payload
        # (we hold it until we have the answer).
        "perm_pending": None,
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

    def drain_queue_one() -> None:
        """Pop one queued line and send it as the next user_prompt.

        Called when a turn finishes and the queue isn't empty. Only
        one drain per turn — the next turn ends, we drain again.
        Subagent async replies (already injected by the agent on its
        next turn boundary via `pending_async_replies`) are NOT in
        this queue; they ride a separate path.
        """
        if not queue:
            return
        next_line = queue.popleft()
        if not send_or_die("user_prompt", prompt=next_line):
            return
        agents_state.setdefault("root", {"status": "thinking"})["status"] = (
            "thinking"
        )
        state["turn_busy"] = True

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
                # Pause turn-busy "thinking" UX, ask user inline. The
                # main prompt is repurposed: the next submission is
                # the y/n/a answer.
                state["perm_pending"] = {
                    "target": event["target"],
                    "agent_id": agent_id,
                }
                console.print(
                    f"\n{_agent_label(agent_id)}"
                    f"[yellow]access requested OUTSIDE workspace:[/yellow]\n"
                    f"  workspace: {permissions.workspace()}\n"
                    f"  target:    {event['target']}\n"
                    f"[yellow]answer at the prompt: y / n / a[/yellow]"
                )
            elif kind == "turn_complete" and agent_id is None:
                state["turn_busy"] = False
                drain_queue_one()
                # If we didn't drain into a new turn, the agent is
                # idle. Reset root status so the always-on footer
                # stops claiming "thinking…" while we wait for the
                # next user input.
                if not state["turn_busy"]:
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
                    # Non-fatal root error: turn is over, queue
                    # freezes (per issue #42 semantics — surface
                    # the error, let the user decide whether to
                    # keep going).
                    state["turn_busy"] = False
            elif kind in ("usage", "checklist"):
                # State already updated; no inline render. Footer
                # picks it up on the next bottom_toolbar refresh.
                pass
            else:
                _print_event(event)
            # Trigger a footer redraw.
            pt_session.app.invalidate()

    def bottom_toolbar() -> ANSI:
        return ANSI(
            _render_status_ansi(
                agents_state,
                state["model"],
                queue,
                state["perm_pending"]["target"] if state["perm_pending"] else None,
            )
        )

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def _esc(event: Any) -> None:
        # Esc means "cancel the in-flight turn AND clear queue" when
        # busy. When idle, no-op (don't interfere with line editing).
        if not state["turn_busy"]:
            return
        send_or_die("cancel")
        if queue:
            queue.clear()
        # Also clear any pending permission state — the agent will
        # tear down whatever was in flight.
        state["perm_pending"] = None
        pt_session.app.invalidate()

    # prompt_toolkit's default `class:bottom-toolbar` style sets a
    # bright background that competes with the embedded ANSI colors
    # in `_render_status_ansi`. Drop the bg + fg overrides so the
    # toolbar inherits the terminal's default background and our
    # rich-emitted color codes do all the talking.
    pt_style = Style.from_dict({
        "bottom-toolbar": "noreverse bg:default fg:default",
        "bottom-toolbar.text": "noreverse bg:default fg:default",
    })

    pt_session: PromptSession = PromptSession(
        history=input_history,
        bottom_toolbar=bottom_toolbar,
        refresh_interval=0.5,
        key_bindings=bindings,
        style=pt_style,
    )

    loop.add_reader(parent_conn.fileno(), on_pipe)
    try:
        while True:
            try:
                with patch_stdout(raw=True):
                    line = await pt_session.prompt_async(_prompt_message())
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D / Ctrl-C at the prompt — clean exit.
                console.print()
                return "eof"
            if state["fatal"]:
                return "fatal"
            stripped = (line or "").strip()
            if not stripped:
                continue
            # Permission-pending mode: route the answer back over
            # the pipe instead of treating the line as a prompt.
            if state["perm_pending"] is not None:
                answer = stripped.lower()
                if answer in ("y", "yes", "n", "no", "a", "always"):
                    decision = answer in ("y", "yes", "a", "always")
                    always = answer in ("a", "always")
                    target = state["perm_pending"]["target"]
                    pending_agent_id = state["perm_pending"]["agent_id"]
                    state["perm_pending"] = None
                    if always:
                        permissions.pre_approve(target)
                    if not send_or_die(
                        "permission_response",
                        decision=decision,
                        always=always,
                        agent_id=pending_agent_id,
                    ):
                        return "fatal"
                else:
                    console.print(
                        f"[red]unrecognized: {answer!r} — please answer "
                        f"y, n, or a[/red]"
                    )
                continue
            # Slash commands (always processed locally, never queued).
            if stripped.startswith("/queue"):
                _handle_queue_command(stripped, queue)
                continue
            if stripped.startswith("/model"):
                state["model"] = _handle_model_command(
                    parent_conn, stripped, state["model"]
                )
                continue
            if stripped == "/tasks":
                _print_tasks(agents_state)
                continue
            # Either dispatch to the agent (idle) or queue (busy).
            if state["turn_busy"]:
                queue.append(line)
                preview = line if len(line) <= 60 else line[:57] + "..."
                console.print(f"[dim grey42]>> queued: {preview}[/dim grey42]")
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

    if resume_id:
        session = Session(session_id=resume_id)
        if not session.exists():
            raise click.UsageError(f"session {resume_id!r} not found at {session.dir}")
    else:
        session = Session()

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
