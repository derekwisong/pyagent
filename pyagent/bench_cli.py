"""`pyagent-bench` — token-efficiency benchmarking harness.

Drives a real LLM-backed agent through a fixed scenario (sequence of
prompts) and reports cumulative token / cost / tool-call totals plus
the session id so the human can follow up with `pyagent-sessions
audit <id>` for a per-turn breakdown.

Run:

    pyagent-bench list                       # show available scenarios
    pyagent-bench run --scenario well_mako   # default Sonnet, $0.50 cap

`run` spawns the same `agent_proc.child_main` subprocess the CLI uses,
so the tools, plugins, system prompt, and pricing all match real usage.
The bench is a non-interactive `--model` consumer: no readline, no
config-file tweaks, no scenario-side hacks. Whatever the human gets
in the REPL is what the bench measures.

Budget enforcement: after each `usage` event the cumulative cost is
checked against `--budget`. If over, the bench finishes the current
turn (closing any in-flight tool batch) and stops sending new
prompts. Pass `--no-budget` to disable.
"""

from __future__ import annotations

import json
import multiprocessing
import shutil
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import click

from pyagent import agent_proc, config, llms, paths, pricing, protocol
from pyagent.session import Session
from pyagent.sessions_audit import _total_tokens_summary


@dataclass
class Scenario:
    name: str
    description: str
    prompts: list[str]
    tools_hint: list[str] = field(default_factory=list)
    # If set, snapshot this directory into the bench's tmpdir workspace
    # before spawning the agent. `"cwd"` means the directory the user
    # invoked `pyagent-bench` from; an absolute path snapshots that
    # directory specifically. The agent operates on the snapshot, so
    # any edits it makes don't touch the live source. Empty (default)
    # means the workspace stays bare — appropriate for scenarios that
    # source their inputs from the network (e.g. well_mako).
    seed_workspace_from: str = ""


@dataclass
class BenchReport:
    scenario: str
    model: str
    session_id: str
    workspace: str  # absolute path to the run's tmpdir workspace
    reason: str  # "complete" | "budget" | "cancelled" | "error"
    prompts_run: int
    prompts_total: int
    wall_time_s: float
    turn_count: int
    tokens: dict[str, int]
    cost_usd: float | None
    budget_usd: float | None
    tool_counts: dict[str, int]


def _scenario_traversable() -> Any:
    """Return the importlib.resources Traversable for the scenarios pkg.

    Works for both editable (`pip install -e .`) and wheel installs.
    """
    return resources.files("pyagent.bench.scenarios")


def _list_scenario_names() -> list[str]:
    out: list[str] = []
    for entry in _scenario_traversable().iterdir():
        # Traversable.name works for both filesystem and zipimport.
        name = entry.name
        if name.endswith(".toml"):
            out.append(name[: -len(".toml")])
    return sorted(out)


def _load_scenario(name: str) -> Scenario:
    target = _scenario_traversable() / f"{name}.toml"
    if not target.is_file():
        avail = ", ".join(_list_scenario_names()) or "(none)"
        raise click.ClickException(
            f"scenario {name!r} not found. Available: {avail}"
        )
    data = tomllib.loads(target.read_text())
    prompts = [p["text"] for p in data.get("prompts", []) if p.get("text")]
    if not prompts:
        raise click.ClickException(
            f"scenario {name!r} has no [[prompts]] entries."
        )
    return Scenario(
        name=data.get("name", name),
        description=data.get("description", ""),
        prompts=prompts,
        tools_hint=list(data.get("tools", []) or []),
        seed_workspace_from=str(data.get("seed_workspace_from", "") or ""),
    )


def _resolve_bench_model(cli_model: str | None) -> str:
    """Same precedence as the main CLI: --model > config > auto-detect.

    No fallback to a stub model — the bench is expected to hit a real
    provider so the numbers reflect production cost.
    """
    if cli_model:
        return llms.resolve_model(cli_model)
    cfg_default = (config.load().get("default_model") or "").strip()
    if cfg_default:
        return llms.resolve_model(cfg_default)
    detected = llms.auto_detect_provider()
    if detected:
        return llms.resolve_model(detected.name)
    raise click.UsageError(
        "no model selected and no API-key env var is set.\n"
        "Pass --model <provider> (e.g. --model anthropic) or set "
        "ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY."
    )


@dataclass
class _BenchState:
    """Mutable per-run accumulator. Updated by the event loop."""

    tokens: dict[str, int] = field(
        default_factory=lambda: {
            "input": 0,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
        }
    )
    tool_counts: dict[str, int] = field(default_factory=dict)
    cumulative_cost_usd: float | None = None
    halt: bool = False  # set when budget exceeded; finish current turn then stop


# Default per-run budget by model. Sized so a typical scenario can
# complete without halting at the budget cap, sized DOWN for cheap
# models so a runaway Haiku run doesn't quietly cost more than the
# user expected. Override at the CLI with --budget X (or --no-budget).
_DEFAULT_BUDGET_BY_BARE_MODEL: dict[str, float] = {
    "claude-haiku-4-5-20251001": 0.20,
    "claude-sonnet-4-6": 0.50,
    "claude-opus-4-7": 3.00,
    "gpt-4o": 0.50,
    "gpt-4o-mini": 0.10,
    "gemini-2.5-flash": 0.10,
}
_BUDGET_FALLBACK_USD = 0.50


def _default_budget_for(model: str) -> float:
    """Pick a reasonable per-run budget for the resolved model."""
    bare = pricing.model_name(model)
    return _DEFAULT_BUDGET_BY_BARE_MODEL.get(bare, _BUDGET_FALLBACK_USD)


def _build_agent_config(
    model: str, session_id: str, workspace: Path
) -> dict[str, Any]:
    """Mirror the CLI's startup setup but for a non-interactive run.

    `workspace` becomes the agent's cwd. The bench mints a fresh
    tmpdir per run (see `run_cmd`) so write_file targets like
    `bench-output.md` land inside the workspace and don't trip the
    permissions gate, AND so back-to-back runs don't leave artifacts
    in the user's project dir.
    """
    soul = paths.resolve("SOUL.md", override=None, seed="SOUL.md")
    tools_md = paths.resolve("TOOLS.md", override=None, seed="TOOLS.md")
    primer = paths.resolve("PRIMER.md", override=None, seed="PRIMER.md")
    return {
        "cwd": str(workspace.resolve()),
        "model": model,
        "session_id": session_id,
        "soul_path": str(soul),
        "tools_path": str(tools_md),
        "primer_path": str(primer),
        # The bench is non-interactive — out-of-workspace permission
        # prompts would deadlock waiting for stdin. Pre-approve the
        # config dir (matches the main CLI). Users hitting an out-of-
        # workspace path during a bench run will see a permission
        # request event and the bench will refuse it (decision=False).
        "approved_paths": [str(paths.config_dir())],
    }


def _drive(
    parent_conn: Any,
    state: _BenchState,
    model: str,
    budget: float | None,
) -> str:
    """Pump events for one prompt. Returns "complete" | "error" | "budget".

    `complete` = the agent finished the turn cleanly; the bench may
    send another prompt. `error` = a fatal agent_error arrived;
    abort. `budget` = cumulative cost crossed the cap mid-turn; the
    current turn is allowed to finish, then the caller halts.
    """
    while True:
        try:
            event = parent_conn.recv()
        except (EOFError, OSError):
            return "error"
        kind = event.get("type")
        if kind == "assistant_text":
            click.echo(f"[assistant] {event.get('text', '')[:200]}")
        elif kind == "tool_call_started":
            name = event.get("name", "?")
            state.tool_counts[name] = state.tool_counts.get(name, 0) + 1
            click.echo(f"  · tool: {name}")
        elif kind == "tool_result":
            # Bench doesn't render tool results — too noisy.
            pass
        elif kind == "permission_request":
            # Non-interactive: deny anything outside the workspace so
            # the bench doesn't deadlock on stdin. The agent will
            # surface the denial as a tool result and continue.
            try:
                protocol.send(
                    parent_conn,
                    "permission_response",
                    decision=False,
                    always=False,
                )
            except (BrokenPipeError, OSError):
                return "error"
            click.echo(
                f"  [denied permission to {event.get('target', '?')} "
                f"(non-interactive bench)]",
                err=True,
            )
        elif kind == "info":
            click.echo(f"  [info] {event.get('message', '')}", err=True)
        elif kind == "usage":
            for k in ("input", "output", "cache_creation", "cache_read"):
                state.tokens[k] = state.tokens.get(k, 0) + int(
                    event.get(k, 0) or 0
                )
            state.cumulative_cost_usd = pricing.estimate_cost_usd(
                model,
                state.tokens["input"],
                state.tokens["output"],
                state.tokens["cache_creation"],
                state.tokens["cache_read"],
            )
            if (
                budget is not None
                and state.cumulative_cost_usd is not None
                and state.cumulative_cost_usd > budget
            ):
                state.halt = True
        elif kind == "ready":
            # Subagent ready event — informational here.
            pass
        elif kind == "turn_complete":
            return "budget" if state.halt else "complete"
        elif kind == "agent_error":
            kind_name = event.get("kind", "Error")
            msg = event.get("message", "")
            click.echo(f"  [error] {kind_name}: {msg}", err=True)
            if event.get("fatal"):
                return "error"
            # Non-fatal agent_error (e.g. KeyboardInterrupt, transient
            # turn failure): treat as turn_complete equivalent. The
            # bench may still continue with the next prompt.
            return "complete"


def _render_report(report: BenchReport) -> str:
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append(f"BENCH REPORT  scenario={report.scenario}")
    lines.append("=" * 60)
    lines.append(f"model:       {report.model}")
    lines.append(f"workspace:   {report.workspace}")
    lines.append(f"session:     {report.session_id}")
    lines.append(f"reason:      {report.reason}")
    lines.append(
        f"prompts:     {report.prompts_run}/{report.prompts_total}"
    )
    lines.append(f"turns:       {report.turn_count}")
    lines.append(f"wall_time:   {report.wall_time_s:.1f}s")
    t = report.tokens
    # Anthropic-vs-other gate: on OpenAI/Gemini, prompt_tokens already
    # includes the cached count; bundling cache_read would double-count.
    total = _total_tokens_summary(report.model, t)
    lines.append(
        f"tokens:      {total:,} total "
        f"(input {t['input']:,} / output {t['output']:,} / "
        f"cache_w {t['cache_creation']:,} / cache_r {t['cache_read']:,})"
    )
    if report.cost_usd is not None:
        lines.append(f"cost:        ${report.cost_usd:.4f}")
    else:
        lines.append("cost:        (model not in pricing table)")
    if report.budget_usd is not None:
        lines.append(f"budget:      ${report.budget_usd:.2f}")
    if report.tool_counts:
        lines.append("tool_calls:")
        for name, count in sorted(
            report.tool_counts.items(), key=lambda kv: -kv[1]
        ):
            lines.append(f"  {name:20s}  {count}")
    else:
        lines.append("tool_calls:  (none)")
    lines.append("")
    lines.append("To inspect this session in detail:")
    lines.append(f"  cd {report.workspace}")
    lines.append(f"  pyagent-sessions audit {report.session_id}")
    return "\n".join(lines)


@click.group()
def main() -> None:
    """Token-efficiency benchmarks for pyagent."""


@main.command("list")
def list_cmd() -> None:
    """List available scenarios."""
    names = _list_scenario_names()
    if not names:
        click.echo("(no scenarios bundled)")
        return
    for name in names:
        try:
            sc = _load_scenario(name)
            click.echo(f"{name}: {sc.description}")
        except Exception as e:
            click.echo(f"{name}: (failed to load: {e})", err=True)


@main.command("run")
@click.option(
    "--scenario", default="well_mako", show_default=True, help="Scenario name."
)
@click.option(
    "--model",
    default=None,
    help="Provider, optionally with '/model-name'. Default: config.default_model "
    "or auto-detect from API-key env vars.",
)
@click.option(
    "--budget",
    type=float,
    default=None,
    help=(
        "Halt after the first turn whose cumulative cost crosses this "
        "USD cap. Default scales by model: $0.20 Haiku, $0.50 Sonnet, "
        "$3.00 Opus, $0.50 fallback."
    ),
)
@click.option(
    "--no-budget",
    is_flag=True,
    help="Disable the budget cap. Run the full scenario regardless of cost.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Also write the report as JSON to this path.",
)
def run_cmd(
    scenario: str,
    model: str | None,
    budget: float | None,
    no_budget: bool,
    out: Path | None,
) -> None:
    """Run a scenario and print a token / cost / tool report.

    Spawns a real agent subprocess against a real LLM. Costs real
    money. A per-model default budget halts a runaway scenario before
    it gets out of hand; override with `--budget X` or `--no-budget`.
    """
    sc = _load_scenario(scenario)
    resolved_model = _resolve_bench_model(model)
    if budget is None:
        budget = _default_budget_for(resolved_model)
    cap = None if no_budget else budget

    # Each bench run gets a fresh tmpdir as its workspace. write_file
    # calls in the scenario (e.g. "save the analysis to bench-output.md")
    # land inside this dir and pass the workspace gate; the user's
    # project dir stays clean across runs. The session and its
    # attachments live under <tmpdir>/.pyagent/sessions/<id>/, so the
    # parent and child agree on absolute paths even though they have
    # different cwds at construction time.
    workspace = Path(
        tempfile.mkdtemp(prefix=f"pyagent-bench-{sc.name}-")
    )

    # Optionally snapshot a source directory into the workspace. Used
    # by self-audit-style scenarios that need real code or data on
    # disk; the snapshot keeps the agent's edits off the live source.
    if sc.seed_workspace_from:
        if sc.seed_workspace_from == "cwd":
            seed_src = Path.cwd().resolve()
        else:
            seed_src = Path(sc.seed_workspace_from).expanduser().resolve()
        if not seed_src.is_dir():
            raise click.ClickException(
                f"scenario {sc.name!r} seed source {seed_src} "
                f"is not a directory."
            )
        click.echo(f"[bench] seed:      {seed_src} → {workspace}")
        # Don't copy git/venv/cache cruft. .pyagent IS copied so the
        # agent inherits the user's project-tier plugins, skills, and
        # roles; the bench's own session is keyed by id under
        # .pyagent/sessions/ and won't collide with anything carried
        # over.
        shutil.copytree(
            seed_src,
            workspace,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "venv", "node_modules",
                "__pycache__", "*.pyc", "*.egg-info", ".pytest_cache",
            ),
        )

    session_root = workspace / ".pyagent" / "sessions"

    # Mint the session up front so we can print + report the id even
    # if the child never reaches `ready`.
    session = Session(root=session_root)
    click.echo(f"[bench] scenario:  {sc.name} ({sc.description})")
    click.echo(f"[bench] model:     {resolved_model}")
    click.echo(f"[bench] workspace: {workspace}")
    click.echo(f"[bench] session:   {session.id}")
    if cap is not None:
        click.echo(f"[bench] budget:    ${cap:.2f}")
    else:
        click.echo("[bench] budget:    (disabled)")

    agent_config = _build_agent_config(
        resolved_model, session.id, workspace
    )
    state = _BenchState()
    started = time.monotonic()

    # Spawn (not fork) — same context the main CLI uses. daemon=False
    # so any subagents the agent spawns aren't immediately reaped.
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(
        target=agent_proc.child_main,
        args=(agent_config, child_conn),
        name="pyagent-bench-agent",
        daemon=False,
    )
    proc.start()
    child_conn.close()

    reason = "error"
    prompts_run = 0
    try:
        # Wait for ready (or fatal) before sending the first prompt.
        while True:
            try:
                ev = parent_conn.recv()
            except (EOFError, OSError):
                click.echo(
                    "[bench] agent exited before ready", err=True
                )
                return
            if ev.get("type") == "ready":
                break
            if ev.get("type") == "info":
                click.echo(f"  [info] {ev.get('message', '')}", err=True)
                continue
            if ev.get("type") == "agent_error":
                click.echo(
                    f"[bench] bootstrap failed: "
                    f"{ev.get('kind')}: {ev.get('message')}",
                    err=True,
                )
                return

        for prompt_idx, prompt in enumerate(sc.prompts):
            click.echo(f"\n[bench] prompt {prompt_idx + 1}/{len(sc.prompts)}")
            click.echo(f"  > {prompt[:120]}")
            try:
                protocol.send(parent_conn, "user_prompt", prompt=prompt)
            except (BrokenPipeError, OSError):
                reason = "error"
                break
            outcome = _drive(parent_conn, state, resolved_model, cap)
            prompts_run += 1
            if outcome == "error":
                reason = "error"
                break
            if state.halt:
                reason = "budget"
                break
        else:
            # Loop ran to completion without break — every prompt sent.
            reason = "complete"
    except KeyboardInterrupt:
        reason = "cancelled"
        try:
            protocol.send(parent_conn, "cancel")
        except (BrokenPipeError, OSError):
            pass
    finally:
        wall_time = time.monotonic() - started
        try:
            protocol.send(parent_conn, "shutdown")
        except (BrokenPipeError, OSError):
            pass
        proc.join(timeout=10)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)
        try:
            parent_conn.close()
        except Exception:
            pass

        # Count assistant turns from the saved transcript so the report
        # number matches `pyagent-sessions audit`'s view of the same run.
        try:
            history = session.load_history()
            turn_count = sum(
                1 for e in history if isinstance(e, dict) and e.get("role") == "assistant"
            )
        except Exception:
            turn_count = 0

        report = BenchReport(
            scenario=sc.name,
            model=resolved_model,
            session_id=session.id,
            workspace=str(workspace.resolve()),
            reason=reason,
            prompts_run=prompts_run,
            prompts_total=len(sc.prompts),
            wall_time_s=wall_time,
            turn_count=turn_count,
            tokens=state.tokens,
            cost_usd=state.cumulative_cost_usd,
            budget_usd=cap,
            tool_counts=state.tool_counts,
        )
        click.echo("")
        click.echo(_render_report(report))
        if out is not None:
            out.write_text(json.dumps(asdict(report), indent=2))
            click.echo(f"[bench] wrote {out}")


if __name__ == "__main__":
    main()
