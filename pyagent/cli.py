import logging
import multiprocessing
import re
import readline  # imported for side effect: enables line editing and history in input()
import shutil
import sys
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import click
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
from pyagent.cancel import CancelWatcher
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
# State is per-CLI-process (one dict, mutated in place by event-stream
# handlers in `_drive_turn`). It carries across turns so a subagent
# spawned in turn N is still tracked at turn N+1.

_SPAWN_INFO_RE = re.compile(
    r"spawned subagent (?P<name>\S+) \(id=(?P<sid>\S+), depth=\d+\)"
)
_TERM_INFO_RE = re.compile(
    r"terminated subagent \S+ \(id=(?P<sid>[^)]+)\)"
)


# USD per million tokens, (input, output). Models not listed get
# token-only display, no $ amount. Update freely as pricing changes
# — this is a best-effort estimate, not authoritative billing.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gemini-2.5-flash": (0.075, 0.30),
}


def _model_name(model_str: str) -> str:
    """Extract the bare model name from a 'provider/name' string.

    Falls back to the provider's default model (via the llms registry)
    if no `/name` was given so the pricing lookup still works on
    `--model anthropic`.
    """
    _, _, name = llms.resolve_model(model_str).partition("/")
    return name


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


def _estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """USD cost estimate, or None if the model isn't in the pricing
    table. Falls back gracefully on unknown / future models — the
    footer renders just the token count in that case."""
    name = _model_name(model)
    if not name:
        # Model defaulted from provider; the AnthropicClient/etc set
        # `client.model` but the CLI doesn't see that here. Skip cost.
        return None
    rates = _PRICING_USD_PER_MTOK.get(name)
    if rates is None:
        return None
    in_rate, out_rate = rates
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


def _format_usage_suffix(
    input_tokens: int, output_tokens: int, model: str
) -> str:
    """Build the ` [Nk tok / $0.0X]` suffix for the status footer.

    Empty string when there's nothing to show (no LLM calls yet).
    """
    total = input_tokens + output_tokens
    if total == 0:
        return ""
    if total >= 1000:
        tok_str = f"{total / 1000:.1f}k tok"
    else:
        tok_str = f"{total} tok"
    cost = _estimate_cost_usd(model, input_tokens, output_tokens)
    if cost is None:
        return f" [{tok_str}]"
    if cost < 0.01:
        cost_str = f"${cost:.4f}"
    else:
        cost_str = f"${cost:.3f}"
    return f" [{tok_str} / {cost_str}]"


def _agents_tokens(agents: dict) -> tuple[int, int]:
    """Sum input/output tokens across all tracked agents."""
    in_tot = sum(a.get("tokens", {}).get("input", 0) for a in agents.values())
    out_tot = sum(a.get("tokens", {}).get("output", 0) for a in agents.values())
    return in_tot, out_tot


def _render_status(agents: dict, model: str = "") -> str:
    """Return the rich-markup string for the status footer.

    Single-agent (only root) → the classic `thinking…` text so the UI
    is unchanged for users not using subagents, plus a token/cost
    suffix once any LLM calls have happened.

    Multi-agent → `agent(status) │ agent(status) │ …` separated by box-
    drawing pipes. Order is insertion order (root first, then
    subagents in spawn order) which gives a stable left-to-right read.
    Token/cost suffix is the aggregate across the whole tree.
    """
    in_tot, out_tot = _agents_tokens(agents)
    suffix = _format_usage_suffix(in_tot, out_tot, model)
    if len(agents) <= 1:
        status = agents.get("root", {}).get("status", "thinking")
        return f"[dim]{status}…{suffix}[/dim]"
    parts = []
    for key, info in agents.items():
        label = "root" if key == "root" else key
        parts.append(f"{label}([cyan]{info['status']}[/cyan])")
    body = " [/dim][dim]│[/dim] [dim]".join(parts)
    return f"[dim]{body}{suffix}[/dim]"


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
    if kind == "usage":
        slot = agents.setdefault(key, {"status": "idle"})
        tokens = slot.setdefault("tokens", {"input": 0, "output": 0})
        tokens["input"] += int(event.get("input", 0) or 0)
        tokens["output"] += int(event.get("output", 0) or 0)
        return


# Optional safety-net pass at session end. Organic ledger work is
# supposed to happen mid-conversation (see SOUL.md), so this sweep is
# off by default — opt in with --memory-pass-on-exit when you want it.
_END_OF_SESSION_PROMPT = (
    "The session is wrapping up. Review this conversation: if anything "
    "should have been recorded in your USER or MEMORY ledger and wasn't, "
    "save it now via read_ledger / write_ledger. Most sessions have "
    "nothing to add — extraction isn't the goal. Make small surgical "
    "edits when you do save; don't rewrite wholesale.\n\n"
    "Reply with ONE short line and nothing else. Examples: "
    "'Nothing new.' / 'Updated USER.' / 'Saved 1 memory.' "
    "No preamble, no recap, no flourish, no voice — just the fact."
)


def _seed_input_history(conversation: list[Any]) -> None:
    """Populate readline's in-memory history from prior user prompts so
    up/down arrow at the input cycles through what was typed before.
    Filters to user messages with string content; tool-result turns
    are skipped because they don't have a `content` string.
    """
    for entry in conversation:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content")
        if not isinstance(content, str):
            continue
        readline.add_history(content)


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


def _prompt_permission(
    target: str, agent_id: str | None = None
) -> tuple[bool, bool]:
    """Run the y/n/a prompt for an out-of-workspace path. Returns
    (decision, always). When `agent_id` is set, the request came from
    a subagent — name it in the prompt so the human knows which agent
    they're authorizing."""
    who = (
        f"Subagent {agent_id!r}"
        if agent_id
        else "Agent"
    )
    sys.stderr.write(
        f"\n{who} is requesting access OUTSIDE the workspace:\n"
        f"  workspace: {permissions.workspace()}\n"
        f"  target:    {target}\n"
    )
    while True:
        sys.stderr.write(
            "Allow? [y]es / [n]o / [a]lways (this path and below): "
        )
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line:  # EOF
            return False, False
        answer = line.strip().lower()
        if answer in ("y", "yes"):
            return True, False
        if answer in ("n", "no"):
            return False, False
        if answer in ("a", "always"):
            return True, True
        sys.stderr.write(
            f"  unrecognized: {answer!r} — please answer y, n, or a\n"
        )


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


def _drive_turn(
    parent_conn: Connection,
    watcher: CancelWatcher,
    pause_io: Any,
    resume_io: Any,
    status: Any,
    agents: dict[str, dict[str, str]],
    model: str = "",
) -> str:
    """Pump events from the child until the current turn finishes.

    Returns one of: "complete", "interrupted", "error", "fatal".

    `status` is the rich Status (the spinner) — we update its message
    on every event so the footer reflects current activity. `agents`
    is the per-agent state dict, mutated in place across turns.
    """
    while True:
        # Poll so we can periodically check the local cancel_event for
        # Esc forwarding. 100ms is short enough that an Esc feels
        # responsive without burning CPU between events.
        if not parent_conn.poll(0.1):
            if watcher.cancel_event.is_set():
                try:
                    protocol.send(parent_conn, "cancel")
                except (BrokenPipeError, OSError):
                    return "fatal"
                watcher.reset()
            continue
        try:
            event = parent_conn.recv()
        except (EOFError, OSError):
            return "fatal"
        _update_agents_state(agents, event)
        try:
            status.update(_render_status(agents, model))
        except Exception:
            # Status rendering errors must never break the event loop.
            logger.exception("status update failed")
        kind = event.get("type")
        agent_id = event.get("agent_id")
        if kind == "assistant_text":
            _on_text(event["text"], agent_id=agent_id)
        elif kind == "tool_call_started":
            _on_tool_call(event["name"], event["args"], agent_id=agent_id)
        elif kind == "tool_result":
            _on_tool_result(event["name"], event["content"], agent_id=agent_id)
        elif kind == "permission_request":
            pause_io()
            try:
                decision, always = _prompt_permission(
                    event["target"], agent_id=agent_id
                )
            finally:
                resume_io()
            try:
                protocol.send(
                    parent_conn,
                    "permission_response",
                    decision=decision,
                    always=always,
                    agent_id=agent_id,
                )
            except (BrokenPipeError, OSError):
                return "fatal"
        elif kind == "info":
            label = _agent_label(agent_id)
            console.print(f"{label}[dim]{event['message']}[/dim]")
        elif kind == "usage":
            # Token-counter event. State already updated by
            # _update_agents_state and reflected in the footer; no
            # additional rendering needed here.
            pass
        elif kind == "ready":
            # Sub-agent became ready — purely informational here, the
            # spawning agent's reply queue handles the synchronous
            # spawn handshake.
            label = _agent_label(agent_id)
            console.print(f"{label}[dim]ready[/dim]")
        elif kind == "turn_complete":
            # Subagent's turn-completes are routed away from the CLI
            # event stream by the parent agent's IO thread, but if one
            # ever gets here, treat it as a sibling of complete.
            if agent_id is None:
                return "complete"
        elif kind == "agent_error":
            if agent_id is not None:
                # Subagent error — surface but keep the root turn going.
                console.print(
                    f"{_agent_label(agent_id)}[red]Error:[/red] "
                    f"{event['kind']}: {event['message']}"
                )
                continue
            if event.get("kind") == "KeyboardInterrupt":
                console.print("[dim]interrupted[/dim]")
                ret = "interrupted"
            else:
                console.print(
                    f"[red]Error:[/red] {event['kind']}: {event['message']}"
                )
                ret = "error"
            return "fatal" if event.get("fatal") else ret
        else:
            logger.warning("cli: unknown event type %r", kind)


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
    "--reset-user",
    is_flag=True,
    help="Overwrite <config-dir>/USER.md with the bundled template.",
)
@click.option(
    "--reset-memory",
    is_flag=True,
    help="Overwrite <config-dir>/MEMORY.md with the bundled template. Destructive — wipes long-term memory.",
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
    help="Shortcut: every --reset-* flag together (SOUL, TOOLS, PRIMER, USER, MEMORY, skills).",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt for destructive resets (USER, MEMORY, skills).",
)
@click.option(
    "--memory-pass-on-exit",
    "memory_pass_on_exit",
    is_flag=True,
    help="Run a safety-net memory pass at session end. Off by default — "
    "the agent is expected to record memory organically mid-conversation. "
    "Enable when you want a final sweep for things that may have slipped.",
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
    reset_user: bool,
    reset_memory: bool,
    reset_skills: bool,
    reset_all: bool,
    assume_yes: bool,
    memory_pass_on_exit: bool,
    verbose: bool,
) -> None:
    install_traceback(show_locals=False)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if verbose:
        logging.getLogger("pyagent").setLevel(logging.INFO)

    will_reset_soul = reset_soul or reset_all
    will_reset_tools = reset_tools or reset_all
    will_reset_primer = reset_primer or reset_all
    will_reset_user = reset_user or reset_all
    will_reset_memory = reset_memory or reset_all
    will_reset_skills = reset_skills or reset_all
    any_reset = any(
        (
            will_reset_soul,
            will_reset_tools,
            will_reset_primer,
            will_reset_user,
            will_reset_memory,
            will_reset_skills,
        )
    )

    if any_reset:
        skills_root = paths.config_dir() / "skills"
        skill_dirs: list[Path] = []
        if will_reset_skills and skills_root.exists():
            skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())

        destructive: list[str] = []
        if will_reset_user:
            destructive.append("USER.md (accumulated preferences)")
        if will_reset_memory:
            destructive.append("MEMORY.md (long-term memory)")
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
            (will_reset_user, "USER.md", "USER.md"),
            (will_reset_memory, "MEMORY.md", "MEMORY.md"),
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
    paths.resolve("USER.md", seed="USER.md")
    permissions.pre_approve(paths.config_dir())

    # CLI keeps a read-only view of history for readline seeding and
    # the "resumed N entries" line; the child owns writes during the run.
    prior = session.load_history()
    _seed_input_history(prior)

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
    try:
        # Parent doesn't need the child's end of the pipe; closing it
        # lets the parent's recv() see EOF promptly when the child dies.
        child_conn.close()

        thinking = console.status("[dim]thinking…[/dim]", spinner="dots")
        watcher = CancelWatcher()

        def _pause_io() -> None:
            thinking.stop()
            watcher.stop()

        def _resume_io() -> None:
            watcher.start()
            thinking.start()

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
        turns_run = 0
        # Per-agent state shared across turns. Root starts here;
        # subagents are added/removed as info events flow through
        # _update_agents_state.
        agents_state: dict[str, dict[str, str]] = {
            "root": {"status": "thinking"},
        }

        while True:
            try:
                prompt = input("> ")
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            stripped = prompt.strip()
            if not stripped:
                continue
            if stripped.startswith("/model"):
                model = _handle_model_command(parent_conn, stripped, model)
                continue
            try:
                protocol.send(parent_conn, "user_prompt", prompt=prompt)
            except (BrokenPipeError, OSError):
                console.print("[red]agent subprocess died[/red]")
                break
            watcher.reset()
            # Root is thinking again at the start of each turn.
            agents_state["root"]["status"] = "thinking"
            thinking.update(_render_status(agents_state, model))
            thinking.start()
            watcher.start()
            try:
                outcome = _drive_turn(
                    parent_conn,
                    watcher,
                    pause_io=_pause_io,
                    resume_io=_resume_io,
                    status=thinking,
                    agents=agents_state,
                    model=model,
                )
            finally:
                watcher.stop()
                thinking.stop()
            turns_run += 1
            if outcome == "fatal":
                console.print("[red]agent subprocess exited unexpectedly[/red]")
                break

        if memory_pass_on_exit and turns_run > 0 and proc.is_alive():
            try:
                protocol.send(
                    parent_conn,
                    "user_prompt",
                    prompt=_END_OF_SESSION_PROMPT,
                    persist=False,
                )
            except (BrokenPipeError, OSError):
                pass
            else:
                reflect = console.status(
                    "[dim]reflecting on the session…[/dim]", spinner="dots"
                )
                reflect.start()
                try:
                    _drive_turn(
                        parent_conn,
                        watcher,
                        pause_io=_pause_io,
                        resume_io=_resume_io,
                        status=reflect,
                        agents=agents_state,
                        model=model,
                    )
                except KeyboardInterrupt:
                    console.print("[dim]skipped memory pass[/dim]")
                finally:
                    reflect.stop()
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


if __name__ == "__main__":
    main()
