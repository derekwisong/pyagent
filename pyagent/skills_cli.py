"""`pyagent-skills` — inspect and remove skills.

Bundled skills live under `pyagent/skills/<name>/` in the package and
load directly from there at runtime — no install step. To customize a
bundled skill, copy its directory into `<config-dir>/skills/` (user
scope) or `./.pyagent/skills/` (project scope) and edit. The override
takes precedence at discovery time.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import click

from pyagent import paths
from pyagent import skills as skills_mod


def _installed_root() -> Path:
    return paths.config_dir() / "skills"


@click.group()
def main() -> None:
    """Inspect and remove pyagent skills."""


@main.command("list")
def list_cmd() -> None:
    """List skills across all three tiers, with state annotations."""
    bundled = skills_mod._scan_dir(skills_mod._bundled_root())
    installed = skills_mod._scan_dir(_installed_root())
    local = skills_mod._scan_dir(skills_mod.LOCAL_SKILLS_DIR)
    enabled = skills_mod._enabled_bundled_names()

    overridden_in_bundled = {n for n in bundled if n in installed or n in local}
    overridden_in_installed = {n for n in installed if n in local}

    click.secho("Bundled (ships with pyagent):", bold=True)
    if bundled:
        for skill in sorted(bundled.values(), key=lambda s: s.name):
            state = "enabled" if skill.name in enabled else "disabled"
            tags = [state]
            if skill.name in overridden_in_bundled:
                tags.append("overridden")
            click.echo(f"  [{', '.join(tags)}] {skill.name}: {skill.description}")
    else:
        click.echo("  (none)")
    click.echo()
    click.echo(
        "  Enable a bundled skill by adding its name to "
        "built_in_skills_enabled in config.toml."
    )
    click.echo(
        "  Run `pyagent-config init` to create the file with documented defaults,"
    )
    click.echo(f"  then edit {paths.config_dir() / 'config.toml'}.")
    click.echo()

    click.secho(f"User ({_installed_root()}):", bold=True)
    if installed:
        for skill in sorted(installed.values(), key=lambda s: s.name):
            tag = "  (overridden)" if skill.name in overridden_in_installed else ""
            click.echo(f"  {skill.name}{tag}: {skill.description}")
    else:
        click.echo("  (none)")
    click.echo()

    click.secho(
        f"Project-local ({skills_mod.LOCAL_SKILLS_DIR}, wins all ties):",
        bold=True,
    )
    if local:
        for skill in sorted(local.values(), key=lambda s: s.name):
            click.echo(f"  {skill.name}: {skill.description}")
    else:
        click.echo("  (none)")


@main.command("uninstall")
@click.argument("name", required=False)
@click.option(
    "--local",
    "-l",
    is_flag=True,
    help="Remove from the workspace ./.pyagent/skills/ instead of the user config dir.",
)
@click.option(
    "--all",
    "all_",
    is_flag=True,
    help="Uninstall every skill in the chosen scope.",
)
def uninstall_cmd(name: str | None, local: bool, all_: bool) -> None:
    """Remove a user-installed or project-local skill.

    Cannot remove bundled skills (they ship with the package).
    """
    root = skills_mod.LOCAL_SKILLS_DIR if local else _installed_root()
    scope = "workspace" if local else "user"

    if all_:
        if name:
            raise click.UsageError("pass either <name> or --all, not both.")
        if not root.exists():
            click.echo(f"no {scope}-installed skills.")
            return
        targets = [d for d in root.iterdir() if d.is_dir()]
        if not targets:
            click.echo(f"no {scope}-installed skills.")
            return
        for d in targets:
            shutil.rmtree(d)
            click.echo(f"uninstalled {d.name} from {d}")
        return

    if not name:
        raise click.UsageError("provide a skill <name> or --all.")
    dest = root / name
    if not dest.exists():
        raise click.ClickException(
            f"no {scope}-installed skill named {name!r} at {dest}."
        )
    shutil.rmtree(dest)
    click.echo(f"uninstalled {name} from {dest}")


if __name__ == "__main__":
    main()
