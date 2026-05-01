"""Workspace-local virtualenv discovery and management.

The agent reaches for `pip install` periodically. Without a workspace
venv, those installs would land in whatever interpreter the CLI was
launched against — which PR #45 already guards at the shell level
(refusing system pip / pyenv pollution / sudo). This module is the
positive answer: discover the workspace's `.venv/` (or create one
on first install) and route every install through it.

Discovery order:
  1. `$VIRTUAL_ENV` — if set, the user already activated something;
     respect it.
  2. `<workspace>/.venv` — the conventional name; what most projects
     and editors expect.
  3. `<workspace>/venv` — the older convention; supported for
     compatibility with existing workspaces.
  4. None — defer creation until something actually needs to
     install. A workspace that never installs anything stays
     venv-free.

Auto-creation uses the running CLI's interpreter (`sys.executable`),
so the agent's Python matches the user's. Installation routes via
`<venv>/bin/pip`.

Issue #46.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _bin_dir(venv: Path) -> Path:
    """Cross-platform bin/Scripts dir inside a venv. Linux/macOS use
    `bin`, Windows uses `Scripts`."""
    return venv / ("Scripts" if os.name == "nt" else "bin")


def pip_path(venv: Path) -> Path:
    """Path to the venv's `pip` executable."""
    name = "pip.exe" if os.name == "nt" else "pip"
    return _bin_dir(venv) / name


def python_path(venv: Path) -> Path:
    """Path to the venv's `python` executable."""
    name = "python.exe" if os.name == "nt" else "python"
    return _bin_dir(venv) / name


def is_venv(path: Path) -> bool:
    """True iff `path` looks like a venv (has bin/python or Scripts/python.exe)."""
    return path.is_dir() and python_path(path).exists()


def discover(workspace: Path) -> Path | None:
    """Return the venv this agent should use, or None if there isn't one yet.

    Priority:
      1. `$VIRTUAL_ENV` (when it points at a real venv)
      2. `<workspace>/.venv`
      3. `<workspace>/venv`

    Symlinks are resolved so two CLI processes pointing at the same
    workspace from different paths agree on which venv to share.
    """
    env_venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if env_venv:
        p = Path(env_venv)
        if is_venv(p):
            return p.resolve()
        # Stale env var (venv was deleted) — fall through to workspace
        # discovery rather than refusing.
        logger.warning(
            "VIRTUAL_ENV=%s is set but does not look like a real venv; "
            "ignoring and falling back to workspace discovery",
            env_venv,
        )

    for name in (".venv", "venv"):
        candidate = workspace / name
        if is_venv(candidate):
            return candidate.resolve()
    return None


def ensure(workspace: Path) -> tuple[Path, bool]:
    """Discover an existing venv or create `<workspace>/.venv`.

    Returns `(venv_path, created)` where `created` is True iff a new
    venv was just created (so the caller can surface a one-line
    notice). The new venv uses `sys.executable`, matching the
    interpreter the CLI is running under.

    Raises `RuntimeError` if creation fails — caller decides how to
    surface (likely as a tool-result error marker).
    """
    existing = discover(workspace)
    if existing is not None:
        return existing, False

    target = workspace / ".venv"
    logger.info(
        "creating workspace venv at %s using %s", target, sys.executable
    )
    try:
        # `--without-pip` would be faster but then we have to
        # bootstrap pip ourselves; default behavior installs pip.
        subprocess.run(
            [sys.executable, "-m", "venv", str(target)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"venv creation failed (rc={e.returncode}): "
            f"{(e.stderr or e.stdout or '').strip()[:500]}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"venv creation timed out after 120s at {target}"
        ) from e

    if not is_venv(target):
        raise RuntimeError(
            f"venv creation reported success but {target} doesn't "
            f"look like a venv afterward"
        )
    return target.resolve(), True


def describe(workspace: Path) -> str:
    """One-line description for the environment footer.

    Returns one of:
      - `<workspace>/.venv  (active)`           — VIRTUAL_ENV matches
      - `<workspace>/.venv  (workspace)`        — found but not activated
      - `none — created on first install`       — nothing yet
    Path is shown relative to workspace when possible (less noise in
    the prompt).
    """
    found = discover(workspace)
    if found is None:
        return "none — created on first pip_install"
    try:
        rel = found.relative_to(workspace.resolve())
        display = str(rel) if str(rel) != "." else str(found)
    except ValueError:
        display = str(found)
    env_venv = os.environ.get("VIRTUAL_ENV", "").strip()
    if env_venv and Path(env_venv).resolve() == found:
        suffix = "(active)"
    else:
        suffix = "(workspace)"
    return f"{display}  {suffix}"
