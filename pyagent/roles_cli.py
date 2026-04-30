"""`pyagent-roles` — list, inspect, seed, and migrate role files.

Roles live as `<name>.md` files in three tiers (matching skills /
plugins / config layout):

  1. Bundled    — `pyagent/roles_bundled/*.md` (ships with pyagent).
  2. User       — `<config-dir>/roles/*.md` (your personal library).
  3. Project    — `./.pyagent/roles/*.md` (per-repo overrides).

Sub-commands:

  list                    — all roles, by tier, name + description.
  show <name>             — render one role's resolved content.
  path <name>             — print the resolved file path (scripting helper).
  init                    — seed <config-dir>/roles/ with the bundled
                            starter set (idempotent).
  migrate                 — convert any [models.<name>] in config.toml
                            into <config-dir>/roles/<name>.md files.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

import click

from pyagent import config as config_mod
from pyagent import paths
from pyagent import roles as roles_mod


def _user_root() -> Path:
    return paths.config_dir() / "roles"


def _project_root() -> Path:
    return roles_mod.LOCAL_ROLES_DIR


@click.group()
def main() -> None:
    """Inspect, seed, and migrate pyagent roles."""
    # Surface roles-module warnings (e.g. legacy [models.<name>]) to
    # stderr so `pyagent-roles list` actually shows them.
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s: %(message)s"
    )


def _render_tier(label: str, root: Path | None, roles: dict) -> None:
    click.secho(f"{label}:", bold=True)
    if not roles:
        click.echo("  (none)")
        return
    for role in sorted(roles.values(), key=lambda r: r.name):
        model_label = role.model or "(inherits parent)"
        click.echo(f"  {role.name} [{model_label}]: {role.description}")


@main.command("list")
def list_cmd() -> None:
    """List roles across all three tiers (plus any legacy TOML roles)."""
    bundled_root = roles_mod._bundled_root()
    bundled = (
        roles_mod._scan_dir(bundled_root) if bundled_root else {}
    )
    user = roles_mod._scan_dir(_user_root())
    project = roles_mod._scan_dir(_project_root())
    legacy = roles_mod._legacy_roles()

    _render_tier(
        "Bundled (ships with pyagent)",
        bundled_root,
        bundled,
    )
    click.echo()
    _render_tier(f"User ({_user_root()})", _user_root(), user)
    click.echo()
    _render_tier(
        f"Project-local ({_project_root()}, wins all ties)",
        _project_root(),
        project,
    )

    if legacy:
        click.echo()
        click.secho(
            "Legacy [models.<name>] in config.toml (deprecated):",
            bold=True,
            fg="yellow",
        )
        for role in sorted(legacy.values(), key=lambda r: r.name):
            click.echo(f"  {role.name} [{role.model}]: {role.description}")
        click.echo()
        click.echo("  Run `pyagent-roles migrate` to convert these to .md files.")


@main.command("show")
@click.argument("name")
def show_cmd(name: str) -> None:
    """Print one role's resolved content (frontmatter-style header + body)."""
    normalized = roles_mod._normalize_name(name)
    role = roles_mod.load().get(normalized)
    if role is None:
        raise click.ClickException(
            f"no role named {name!r}; try `pyagent-roles list`."
        )
    src = str(role.source) if role.source else "(legacy: config.toml [models.*])"
    click.echo(f"# name:        {role.name}")
    click.echo(f"# source:      {src}")
    click.echo(f"# model:       {role.model or '(inherits parent)'}")
    click.echo(f"# tools:       {list(role.tools) if role.tools is not None else '(default set)'}")
    click.echo(f"# meta_tools:  {role.meta_tools}")
    click.echo(f"# description: {role.description}")
    click.echo()
    click.echo(role.system_prompt)


@main.command("path")
@click.argument("name")
def path_cmd(name: str) -> None:
    """Print the absolute path of the file that defines a role."""
    normalized = roles_mod._normalize_name(name)
    role = roles_mod.load().get(normalized)
    if role is None:
        raise click.ClickException(
            f"no role named {name!r}; try `pyagent-roles list`."
        )
    if role.source is None:
        raise click.ClickException(
            f"role {name!r} comes from config.toml [models.*]; "
            "run `pyagent-roles migrate` to give it a file path."
        )
    click.echo(str(role.source))


@main.command("init")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing files in <config-dir>/roles/. Off by default.",
)
def init_cmd(force: bool) -> None:
    """Seed <config-dir>/roles/ with the bundled starter set.

    Idempotent: skips files that already exist unless --force. The
    bundled set is intended as a starting library — copy and edit
    freely; tier precedence (project > user > bundled) means your
    edits win at lookup time.
    """
    target_root = _user_root()
    target_root.mkdir(parents=True, exist_ok=True)

    bundled_pkg = resources.files(roles_mod.PACKAGE_ROLES_PKG)
    written = 0
    skipped = 0
    for entry in sorted(bundled_pkg.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".md"):
            continue
        dest = target_root / entry.name
        if dest.exists() and not force:
            click.echo(f"skipped {dest} (exists)")
            skipped += 1
            continue
        dest.write_text(entry.read_text())
        click.echo(f"wrote {dest}")
        written += 1
    click.echo(f"\ndone: {written} written, {skipped} skipped.")
    if skipped and not force:
        click.echo("(pass --force to overwrite existing files)")


def _toml_value(v: object) -> str:
    """Render a Python value as TOML for the migrated frontmatter."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported toml value: {v!r}")


def _render_migrated_role(role: roles_mod.Role, original_name: str) -> str:
    """Build the .md file text for a migrated role."""
    fm_lines = ["+++"]
    if role.model:
        fm_lines.append(f"model = {_toml_value(role.model)}")
    if role.tools is not None:
        fm_lines.append(f"tools = {_toml_value(list(role.tools))}")
    if role.meta_tools is not True:
        fm_lines.append(f"meta_tools = {_toml_value(role.meta_tools)}")
    fm_lines.append(f"description = {_toml_value(role.description)}")
    fm_lines.append("+++")
    body = role.system_prompt.strip()
    if not body:
        body = f"# Role: {original_name}\n\n(No persona body in the original [models.<name>] entry.)"
    return "\n".join(fm_lines) + "\n\n" + body + "\n"


@main.command("migrate")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing .md files in <config-dir>/roles/. Off by default.",
)
def migrate_cmd(force: bool) -> None:
    """Convert [models.<name>] entries in config.toml to .md files.

    Writes each migrated role to `<config-dir>/roles/<NAME>.md`. Does
    NOT touch the original config.toml — you can delete the
    `[models.<name>]` blocks once you've verified the .md files
    behave as expected.
    """
    cfg = config_mod.load()
    raw = cfg.get("models", {})
    if not isinstance(raw, dict) or not raw:
        click.echo("no [models.<name>] entries found in config.toml.")
        return

    target_root = _user_root()
    target_root.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    for name, entry in raw.items():
        role = roles_mod._coerce_legacy_role(name, entry)
        if role is None:
            continue
        dest = target_root / f"{name.upper()}.md"
        if dest.exists() and not force:
            click.echo(f"skipped {dest} (exists)")
            skipped += 1
            continue
        dest.write_text(_render_migrated_role(role, name))
        click.echo(f"wrote {dest}")
        written += 1

    click.echo(f"\nmigrated {written} role(s), skipped {skipped}.")
    click.echo(
        "The original [models.<name>] blocks in config.toml are untouched. "
        "Once you've verified the migrated .md files, delete them from "
        "config.toml to silence the deprecation warning."
    )


if __name__ == "__main__":
    main()
