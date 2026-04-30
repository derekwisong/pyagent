"""`pyagent-sessions` — manage saved chat sessions in this directory.

Sessions live at `<root>/<id>/` where root defaults to
`.pyagent/sessions/`. This script lists them with size/activity
metadata and provides delete/prune for cleanup. Bulk deletes
default to dry-run for safety; pass `--no-dry-run` to actually
remove anything.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import click

from pyagent import config
from pyagent.session import Session
from pyagent.sessions_audit import audit_session
from pyagent.sessions_audit_render import (
    ALL_SECTIONS,
    render_json,
    render_text,
)


def _root() -> Path:
    return Session.DEFAULT_ROOT


def _session_dirs(root: Path) -> list[Path]:
    """All session dirs, newest first by modification time."""
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _info(d: Path) -> dict[str, object]:
    conv = d / "conversation.jsonl"
    turns = 0
    if conv.exists():
        with conv.open() as f:
            turns = sum(1 for line in f if line.strip())
    total_size = sum(
        f.stat().st_size for f in d.rglob("*") if f.is_file()
    )
    return {
        "id": d.name,
        "mtime": d.stat().st_mtime,
        "turns": turns,
        "size": total_size,
    }


def _humanize_size(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}T"


def _humanize_age(mtime: float) -> str:
    delta = time.time() - mtime
    days = int(delta // 86400)
    if days >= 1:
        return f"{days}d ago"
    hours = int(delta // 3600)
    if hours >= 1:
        return f"{hours}h ago"
    minutes = int(delta // 60)
    return f"{minutes}m ago" if minutes >= 1 else "just now"


@click.group()
def main() -> None:
    """Manage pyagent chat sessions in this directory."""


@main.command("list")
def list_cmd() -> None:
    """List sessions in ./.pyagent/sessions/, newest first."""
    root = _root()
    dirs = _session_dirs(root)
    if not dirs:
        click.echo(f"no sessions in {root}.")
        return
    for d in dirs:
        info = _info(d)
        click.echo(
            f"{info['id']:35s}  "
            f"{_humanize_age(info['mtime']):>10s}  "
            f"{info['turns']:>4d} turns  "
            f"{_humanize_size(info['size']):>6s}"
        )


@main.command("delete")
@click.argument("session_id", required=False)
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="Delete every session in this project.",
)
@click.option(
    "--dry-run",
    "-n",
    is_flag=True,
    help="Preview without deleting.",
)
def delete_cmd(session_id: str | None, all_: bool, dry_run: bool) -> None:
    """Delete a session by id, or every session with --all."""
    root = _root()
    verb = "would delete" if dry_run else "deleted"

    if all_:
        if session_id:
            raise click.UsageError(
                "pass either <session_id> or --all, not both."
            )
        dirs = _session_dirs(root)
        if not dirs:
            click.echo(f"no sessions in {root}.")
            return
        for d in dirs:
            if not dry_run:
                shutil.rmtree(d)
            click.echo(f"{verb} {d}")
        return
    if not session_id:
        raise click.UsageError("provide a session_id or --all.")
    target = root / session_id
    if not target.exists():
        raise click.ClickException(f"no session {session_id!r} in {root}.")
    if not dry_run:
        shutil.rmtree(target)
    click.echo(f"{verb} {target}")


@main.command("prune")
@click.option(
    "--older-than",
    type=int,
    metavar="DAYS",
    help="Delete sessions whose last activity is older than DAYS days.",
)
@click.option(
    "--keep",
    type=int,
    metavar="N",
    help="Keep the N most recent sessions; delete the rest.",
)
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="Delete every session in this project.",
)
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Preview only; pass --no-dry-run to actually delete.",
)
def prune_cmd(
    older_than: int | None, keep: int | None, all_: bool, dry_run: bool
) -> None:
    """Bulk-delete sessions matching one selector. Dry-run by default."""
    selectors = [older_than is not None, keep is not None, all_]
    if sum(selectors) != 1:
        raise click.UsageError(
            "provide exactly one of --older-than, --keep, --all."
        )

    root = _root()
    dirs = _session_dirs(root)
    if not dirs:
        click.echo(f"no sessions in {root}.")
        return

    if all_:
        targets = dirs
    elif older_than is not None:
        cutoff = time.time() - older_than * 86400
        targets = [d for d in dirs if d.stat().st_mtime < cutoff]
    else:
        # --keep N: dirs is sorted newest-first; keep first N, delete rest.
        targets = dirs[keep:]

    if not targets:
        click.echo("nothing to prune.")
        return

    verb = "would delete" if dry_run else "deleting"
    click.echo(f"{verb} {len(targets)} session(s):")
    for d in targets:
        info = _info(d)
        click.echo(
            f"  {info['id']}  "
            f"({_humanize_age(info['mtime'])}, "
            f"{_humanize_size(info['size'])}, "
            f"{info['turns']} turns)"
        )
    if dry_run:
        click.echo("\n(dry run — pass --no-dry-run to actually delete)")
        return
    for d in targets:
        shutil.rmtree(d)
    click.echo(f"deleted {len(targets)} session(s).")


@main.command("audit")
@click.argument("session_id")
@click.option(
    "--model",
    default=None,
    help=(
        "Model used for cost estimation. Default: config.default_model, "
        "else anthropic/claude-sonnet-4-6."
    ),
)
@click.option("--cost-only", "-c", is_flag=True, help="Show header only.")
@click.option(
    "--turns-only", "-t", is_flag=True, help="Show per-turn table only."
)
@click.option(
    "--attachments-only",
    "-a",
    is_flag=True,
    help="Show attachments section only.",
)
@click.option(
    "--bloat-only", "-b", is_flag=True, help="Show inline-bloat section only."
)
@click.option("--json", "json_out", is_flag=True, help="Emit JSON instead of text.")
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    help="Suppress lower-bound / fallback warnings.",
)
@click.option(
    "--top",
    type=int,
    default=20,
    show_default=True,
    help="Max bloat rows to show.",
)
def audit_cmd(
    session_id: str,
    model: str | None,
    cost_only: bool,
    turns_only: bool,
    attachments_only: bool,
    bloat_only: bool,
    json_out: bool,
    quiet: bool,
    top: int,
) -> None:
    """Audit a session: cost, per-turn tokens, attachments, inline bloat."""
    root = _root()
    target = root / session_id
    if not target.exists():
        raise click.ClickException(f"no session {session_id!r} in {root}.")

    # Resolve which sections the user wants. Default = all four. Any
    # `--*-only` flag narrows to that one section. Multiple `-only`
    # flags compose (so `-c -a` shows cost + attachments).
    sections: set[str]
    only_flags = {
        "cost": cost_only,
        "turns": turns_only,
        "attachments": attachments_only,
        "bloat": bloat_only,
    }
    if any(only_flags.values()):
        sections = {k for k, v in only_flags.items() if v}
    else:
        sections = set(ALL_SECTIONS)

    # Resolve model: --model > config.default_model > sonnet fallback.
    if model:
        resolved_model = model
    else:
        cfg_default = (config.load().get("default_model") or "").strip()
        if cfg_default:
            resolved_model = cfg_default
        else:
            resolved_model = "anthropic/claude-sonnet-4-6"
            if not quiet:
                click.echo(
                    f"[note] no --model and no config.default_model — "
                    f"using {resolved_model} for cost estimate.",
                    err=True,
                )

    report = audit_session(target, model=resolved_model, top_bloat=top)

    if json_out:
        click.echo(render_json(report))
        return
    click.echo(render_text(report, sections=sections, top=top, quiet=quiet))


if __name__ == "__main__":
    main()
