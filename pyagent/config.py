"""Project configuration loaded from a TOML file.

Lives at `<config-dir>/config.toml`. Missing or unreadable files fall
back to `DEFAULTS`. User keys are deep-merged over the defaults so
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

DEFAULTS: dict[str, Any] = {
    "default_model": "",
    "built_in_skills_enabled": ["write-skill"],
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


def load() -> dict[str, Any]:
    """Return effective config: DEFAULTS with user overrides merged in.

    A missing or malformed config.toml is logged and ignored — agent
    runs should never be blocked by a typo in user config.
    """
    cfg_path = paths.config_dir() / CONFIG_FILENAME
    if not cfg_path.exists():
        return _deep_merge(DEFAULTS, {})  # fresh copy
    try:
        with cfg_path.open("rb") as f:
            user = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("config.toml at %s unreadable: %s; using defaults", cfg_path, e)
        return _deep_merge(DEFAULTS, {})
    return _deep_merge(DEFAULTS, user)


def path() -> Path:
    """Where the config file lives (whether or not it exists)."""
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
    then tables — the order TOML requires.

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
        lines.append("")
        lines.append(f"{prefix}[{k}]")
        for k2, v2 in v.items():
            lines.append(f"{prefix}{k2} = {_render_toml_value(v2)}")
    return "\n".join(lines) + "\n"


def init_default(force: bool = False) -> tuple[Path, bool]:
    """Write the documented default schema to the config file.

    Returns (path, written). If the file already exists and `force` is
    False, returns (path, False) without touching it.
    """
    target = path()
    if target.exists() and not force:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_toml(DEFAULTS, commented=True))
    return target, True
