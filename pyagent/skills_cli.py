"""`pyagent-skills` — manage installed skills.

Bundled skills live under `pyagent/skills/<name>/` in this package.
Users opt in by running `pyagent-skills install <name>`, which copies
the bundled directory into `<config-dir>/skills/<name>/` so the agent
discovers it on the next run.
"""

from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

import click

from pyagent import paths
from pyagent import skills as skills_mod

_BUNDLED_PKG = "pyagent.skills"


def _bundled_root() -> Path:
    """Locate the on-disk path of `pyagent/skills/`. Works for editable
    installs and normal site-packages — both expose a real Path.
    """
    root = resources.files(_BUNDLED_PKG)
    return Path(str(root))


def _bundled_skills() -> dict[str, skills_mod.Skill]:
    return skills_mod._scan_dir(_bundled_root())


def _installed_root() -> Path:
    return paths.config_dir() / "skills"


@click.group()
def main() -> None:
    """Manage pyagent skills (catalog the agent can load on demand)."""


@main.command("list")
def list_cmd() -> None:
    """List bundled and installed skills."""
    bundled = _bundled_skills()
    installed = skills_mod._scan_dir(_installed_root())
    local = skills_mod._scan_dir(skills_mod.LOCAL_SKILLS_DIR)

    def render(label: str, found: dict[str, skills_mod.Skill]) -> None:
        click.secho(label, bold=True)
        if not found:
            click.echo("  (none)")
            return
        for skill in sorted(found.values(), key=lambda s: s.name):
            click.echo(f"  {skill.name}: {skill.description}")

    render("Bundled (run `pyagent-skills install <name>` to enable):", bundled)
    click.echo()
    render(f"Installed ({_installed_root()}):", installed)
    click.echo()
    render(f"Project-local ({skills_mod.LOCAL_SKILLS_DIR}, overrides installed):", local)


@main.command("install")
@click.argument("name")
@click.option(
    "--local",
    "-l",
    is_flag=True,
    help=(
        "Install into the current workspace's ./.pyagent/skills/ "
        "instead of the user config dir. Workspace skills override "
        "config-dir skills of the same name."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing installation of the same skill.",
)
def install_cmd(name: str, local: bool, force: bool) -> None:
    """Copy a bundled skill into the user config dir (or this workspace)."""
    src = _bundled_root() / name
    if not (src / "SKILL.md").exists():
        available = ", ".join(sorted(_bundled_skills())) or "(none)"
        raise click.ClickException(
            f"no bundled skill named {name!r}. available: {available}"
        )
    root = skills_mod.LOCAL_SKILLS_DIR if local else _installed_root()
    dest = root / name
    if dest.exists():
        if not force:
            raise click.ClickException(
                f"{dest} already exists. pass --force to overwrite."
            )
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    scope = "workspace" if local else "user"
    click.echo(f"installed {name} ({scope}) -> {dest}")


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
    """Remove an installed skill."""
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
