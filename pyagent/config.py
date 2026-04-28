"""Project configuration loaded from a TOML file.

Lives at `<config-dir>/config.toml`. Missing or unreadable files fall
back to `DEFAULTS`. User keys are deep-merged over the defaults so
partial overrides are sufficient — you only have to write the keys
you actually want to change.

Schema (current):

    [subagents]
    max_depth  = 3   # maximum spawn-tree height; root is depth 0
    max_fanout = 5   # max simultaneous children any single agent can hold

The caps exist to mitigate fork-bomb behavior — a confused turn could
spawn unboundedly otherwise, amplifying cost per process and outpacing
the human's ability to hit Esc.
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
