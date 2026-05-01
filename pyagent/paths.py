"""Resolve persona/notebook file locations across cwd, config dir, and
bundled defaults.

Resolution priority for a given name (e.g. "SOUL.md"):
  1. Explicit override (if a flag was passed).
  2. ./{name} in cwd, if it exists — handy for per-project overrides
     and for hacking on the prompts inside the pyagent repo itself.
  3. <config_dir>/{name}, seeded from the package's bundled default
     on first run if `seed` was provided.

Two XDG-flavored homes via platformdirs:
  config_dir() — user-edited preferences (config.toml, persona files,
                 roles, skills, plugin manifests).
    Linux:   ~/.config/pyagent/
    macOS:   ~/Library/Application Support/pyagent/
    Windows: %APPDATA%\\pyagent\\
  data_dir()   — agent-generated data (plugin ledgers, sessions,
                 anything irreplaceable that the user doesn't
                 hand-edit).
    Linux:   ~/.local/share/pyagent/
    macOS:   ~/Library/Application Support/pyagent/
    Windows: %LOCALAPPDATA%\\pyagent\\
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import platformdirs

_PACKAGE_DEFAULTS = "pyagent.defaults"


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir("pyagent"))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir("pyagent"))


def resolve(
    name: str, override: Path | None = None, *, seed: str | None = None
) -> Path:
    if override is not None:
        return override
    cwd_path = Path(name)
    if cwd_path.exists():
        return cwd_path
    target = config_dir() / name
    if seed and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        bundled = resources.files(_PACKAGE_DEFAULTS).joinpath(seed)
        if bundled.is_file():
            target.write_text(bundled.read_text())
    return target


def reset_to_default(name: str, seed: str) -> Path:
    """Overwrite the config-dir copy of `name` with the bundled `seed`.

    Always targets `<config_dir>/<name>`, regardless of whether a
    project-local override exists in cwd. Returns the path written.
    Raises FileNotFoundError if the bundled seed isn't packaged.
    """
    target = config_dir() / name
    bundled = resources.files(_PACKAGE_DEFAULTS).joinpath(seed)
    if not bundled.is_file():
        raise FileNotFoundError(f"no bundled default for {seed!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(bundled.read_text())
    return target
