"""Project configuration loaded from TOML files.

Two tiers, both optional:
  - `<config-dir>/config.toml`         — user tier (per-user defaults)
  - `./.pyagent/config.toml`           — project tier (per-repo overrides)

Effective config = DEFAULTS < user < project, deep-merged. Missing or
unreadable files fall back to `DEFAULTS`. Keys are deep-merged so
partial overrides are sufficient — you only have to write the keys
you actually want to change.

Schema (current):

    default_model = ""                          # provider or provider/model
    built_in_skills_enabled = ["write-skill"]   # bundled skills in the catalog

    [subagents]
    max_depth  = 3   # maximum spawn-tree height; root is depth 0
    max_fanout = 5   # max simultaneous children any single agent can hold

The subagent caps exist to mitigate fork-bomb behavior — a confused
turn could spawn unboundedly otherwise, amplifying cost per process
and outpacing the human's ability to hit Esc.

`default_model` pins the provider/model used when `--model` is not
passed. Empty string means auto-detect from API-key env vars.

`built_in_skills_enabled` lists which *built-in* skills (the ones shipped
under `pyagent/skills/` in the package) appear in the catalog. Setting
this key in user config replaces the default list entirely, so include
every built-in skill you want enabled. User-installed skills (under
`<config-dir>/skills/`) and project-local skills (under
`./.pyagent/skills/`) are always discovered regardless of this list —
their presence on disk *is* the user's enablement signal.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from pyagent import paths

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"
LOCAL_CONFIG_DIR = Path(".pyagent")

DEFAULTS: dict[str, Any] = {
    "default_model": "",
    "built_in_skills_enabled": ["write-skill"],
    "built_in_plugins_enabled": ["memory-markdown"],
    "subagents": {
        "max_depth": 3,
        "max_fanout": 5,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into a copy of `base`.

    Scalars and lists in `override` replace the corresponding key in
    `base`. Dicts are merged key-by-key. The originals are not mutated.
    """
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file. Missing returns {}; malformed warns and returns {}."""
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("config.toml at %s unreadable: %s; ignoring", path, e)
        return {}


def load() -> dict[str, Any]:
    """Return effective config: DEFAULTS < user < project.

    Three tiers, deep-merged in order. Project (./.pyagent/config.toml)
    wins over user (~/.config/pyagent/config.toml) which wins over the
    bundled DEFAULTS. A missing or malformed file at any tier is
    logged and treated as empty — agent runs should never be blocked
    by a typo in config.
    """
    user = _read_toml(paths.config_dir() / CONFIG_FILENAME)
    project = _read_toml(LOCAL_CONFIG_DIR / CONFIG_FILENAME)
    return _deep_merge(_deep_merge(DEFAULTS, user), project)


def path() -> Path:
    """Where the user-tier config file lives (whether or not it exists).

    Project-tier config (./.pyagent/config.toml) is read but not
    surfaced through this helper — the CLI commands that edit config
    target the user tier.
    """
    return paths.config_dir() / CONFIG_FILENAME


_TOML_HEADER = (
    "# pyagent configuration\n"
    "# Loaded from this file; missing keys fall back to bundled defaults.\n"
    "# Lists replace (do not merge), so if you set a list, include every\n"
    "# value you want — defaults for that key no longer apply.\n"
)


def _render_toml_value(v: Any) -> str:
    """Render a Python value as TOML. Supports scalars and flat lists."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        escaped = v.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(v, list):
        return "[" + ", ".join(_render_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported toml value: {v!r}")


def render_toml(data: dict[str, Any], commented: bool = False) -> str:
    """Render a config dict as TOML text. Top-level scalars/lists first,
    then tables — the order TOML requires. Tables containing nested
    tables (e.g. `models.<name>` role definitions) render each child
    as its own `[parent.child]` section so `pyagent-config show` works
    with role-defining configs.

    If `commented`, every value line is prefixed with `# ` so the file
    serves as documentation: the user uncomments lines they want to
    override.
    """
    prefix = "# " if commented else ""
    lines = [_TOML_HEADER]
    flat = [(k, v) for k, v in data.items() if not isinstance(v, dict)]
    tables = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    for k, v in flat:
        lines.append(f"{prefix}{k} = {_render_toml_value(v)}")
    for k, v in tables:
        scalars = {kk: vv for kk, vv in v.items() if not isinstance(vv, dict)}
        sub_tables = {kk: vv for kk, vv in v.items() if isinstance(vv, dict)}
        if scalars:
            lines.append("")
            lines.append(f"{prefix}[{k}]")
            for k2, v2 in scalars.items():
                lines.append(f"{prefix}{k2} = {_render_toml_value(v2)}")
        for k2, v2 in sub_tables.items():
            lines.append("")
            lines.append(f"{prefix}[{k}.{k2}]")
            for k3, v3 in v2.items():
                if isinstance(v3, dict):
                    continue  # three-deep nesting not supported
                lines.append(f"{prefix}{k3} = {_render_toml_value(v3)}")
    return "\n".join(lines) + "\n"


_ROLE_EXAMPLE_BLOCK = """
# Roles — named subagent models the orchestrator can address by name.
#
# Each [models.<name>] table defines a preset that `spawn_subagent`
# resolves via its `model` argument. Required: model, description.
# Optional: system_prompt / system_prompt_path (default subagent
# persona body, layered onto SOUL/TOOLS/PRIMER), tools (allowlist
# narrowing the default tool set), meta_tools (default true; set
# false to make a leaf role that can't fan out further).
#
# [models.planner]
# model = "anthropic/claude-opus-4-7"
# description = "Deep reasoning, multi-step planning."
# system_prompt = "You are a planner. Break tasks into steps before recommending edits."
# tools = ["read_file", "grep", "list_directory", "fetch_url"]
# meta_tools = false
"""


def commented_template() -> str:
    """Render the full commented-out template: documented defaults
    plus a commented role example so users can discover the
    `[models.<name>]` schema from `pyagent-config defaults` and the
    file written by `pyagent-config init`.
    """
    return render_toml(DEFAULTS, commented=True) + _ROLE_EXAMPLE_BLOCK


def init_default(force: bool = False) -> tuple[Path, bool]:
    """Write the documented default schema to the config file.

    Returns (path, written). If the file already exists and `force` is
    False, returns (path, False) without touching it.
    """
    target = path()
    if target.exists() and not force:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(commented_template())
    return target, True
