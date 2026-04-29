"""`pyagent-config` — inspect and initialize the pyagent config file.

Three subcommands:

- `show`      print the effective merged config (defaults + user overrides)
- `defaults`  print the bundled defaults as a commented-out TOML template
- `init`      write the commented-out template to the config file if absent

The file lives at `<config-dir>/config.toml`. Missing or empty file means
defaults apply; nothing breaks. `init` exists so a user who wants a
starting point doesn't have to copy-paste from the README.
"""

from __future__ import annotations

import click

from pyagent import config


@click.group()
def main() -> None:
    """Inspect and initialize pyagent's config file."""


@main.command("show")
def show_cmd() -> None:
    """Print the effective merged config (defaults + user overrides)."""
    click.echo(config.render_toml(config.load(), commented=False), nl=False)
    click.echo()
    click.echo(f"# loaded from: {config.path()}", err=True)
    click.echo(
        f"# (file {'exists' if config.path().exists() else 'absent — defaults only'})",
        err=True,
    )


@main.command("defaults")
def defaults_cmd() -> None:
    """Print the bundled defaults as a commented-out TOML template.

    Pipe to a file, or use `pyagent-config init` to drop it in place.
    """
    click.echo(config.commented_template(), nl=False)


@main.command("init")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config.toml. Off by default.",
)
def init_cmd(force: bool) -> None:
    """Create the config file with documented defaults if it doesn't exist.

    Never overwrites — pass --force only if you want to start over.
    The written template has every line commented out, so the file's
    presence does not change behavior. Uncomment the lines you want to
    override.
    """
    target, written = config.init_default(force=force)
    if not written:
        click.echo(f"config.toml already exists at {target}")
        click.echo("(pass --force to overwrite)")
        return
    click.echo(f"wrote default config to {target}")
    click.echo("Edit the file and uncomment lines to override defaults.")


if __name__ == "__main__":
    main()
