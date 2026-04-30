"""`pyagent-plugins` — inspect installed plugins.

Plugins are loaded across three tiers: bundled (shipping with
pyagent), entry-point installed (`pip install pyagent-foo`), and
drop-ins (`<config-dir>/plugins/<name>/` or
`./.pyagent/plugins/<name>/`). Project beats user beats entry-point
beats bundled, by plugin name.
"""

from __future__ import annotations

import click

from pyagent import paths
from pyagent import plugins as plugins_mod


@click.group()
def main() -> None:
    """Inspect installed pyagent plugins."""


@main.command("list")
def list_cmd() -> None:
    """List discovered plugins, with tier and state annotations."""
    records = plugins_mod.discover()
    if not records:
        click.echo("no plugins discovered.")
        click.echo()
        _print_tier_paths()
        return

    by_tier: dict[str, list[plugins_mod.PluginRecord]] = {}
    for r in records:
        by_tier.setdefault(r.tier, []).append(r)

    tier_label = {
        "bundled": "Bundled (ships with pyagent)",
        "entry_point": "Entry-point installed (pip)",
        "user": f"User ({paths.config_dir() / 'plugins'})",
        "project": f"Project-local ({plugins_mod.LOCAL_PLUGINS_DIR}, wins all ties)",
    }

    for tier in ("bundled", "entry_point", "user", "project"):
        items = by_tier.get(tier, [])
        click.secho(f"{tier_label[tier]}:", bold=True)
        if not items:
            click.echo("  (none)")
            click.echo()
            continue
        for record in items:
            m = record.manifest
            tags = [m.version]
            if not record.enabled:
                tags.append("disabled")
            if record.shadowed_by:
                tags.append(
                    f"overrides {len(record.shadowed_by)} earlier tier(s)"
                )
            tools = ", ".join(m.provides_tools) or "(no tools)"
            click.echo(
                f"  {m.name} [{', '.join(tags)}]: {m.description}"
            )
            click.echo(f"    tools: {tools}")
            if record.shadowed_by:
                for path in record.shadowed_by:
                    click.echo(f"    overrides: {path}")
        click.echo()

    _print_tier_paths()


def _print_tier_paths() -> None:
    click.echo(
        "  Enable a bundled plugin by adding its name to "
        "built_in_plugins_enabled in config.toml."
    )
    click.echo(
        "  Disable an entry-point or user-installed plugin via "
        "[plugins.<name>] enabled = false."
    )


if __name__ == "__main__":
    main()
