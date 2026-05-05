"""Introspect (and lazily create) the Python venv for a given scope.

Two scopes the agent cares about:

  - "workspace" (default) — the venv tied to the workspace the user is
    working in. Created lazily on first call so a workspace that never
    needs Python stays venv-free. This is where user-facing deps go:
    `execute("<workspace_venv>/bin/pip install psutil")` and friends.
  - "agent" — the venv pyagent itself is running under (`sys.executable`).
    Used when the agent is doing self-improvement: writing or extending
    a plugin whose imports load into this same Python process. Never
    created — if pyagent isn't in a venv, the call says so honestly
    rather than silently inventing one.

The tool returns paths and a version, *not* a list of installed
packages. The agent invokes `pip list` / `pip show` itself when it
needs that — keeps this tool's output bounded and lets the LLM use
the pip CLI it already knows fluently.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pyagent import venv as venv_mod


_WORKSPACE_PY_VERSION_TIMEOUT_S = 10


def _python_version_for(python_bin: Path) -> str:
    """Return the X.Y.Z version of `python_bin`, or empty on failure.

    A short subprocess — venv pythons start fast — but capped so a
    hung interpreter can't wedge the tool.
    """
    try:
        proc = subprocess.run(
            [
                str(python_bin),
                "-c",
                "import sys; print('.'.join(map(str, sys.version_info[:3])))",
            ],
            capture_output=True,
            text=True,
            timeout=_WORKSPACE_PY_VERSION_TIMEOUT_S,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def make_python_env(workspace: Path):
    """Build the `python_env` tool, closing over the workspace path.

    Factory mirrors the other workspace-aware tools — the workspace
    path comes from `base_config["cwd"]` at registration time, so the
    tool body has it without re-discovering on every call.
    """

    def python_env(scope: str = "workspace") -> str:
        """Report the Python venv for the given scope.

        Args:
            scope: Which venv to report on.
              - `"workspace"` (default): the venv at `<workspace>/.venv`.
                Created if missing — the call is the bootstrap, so the
                first invocation in a fresh workspace is what brings
                the venv into being.
              - `"agent"`: the venv pyagent itself is running under
                (`sys.executable`'s parent venv). Not created — if
                pyagent isn't in a venv, the result reports that
                explicitly.

        Returns:
            A JSON object (as a string) with these keys:

              - `scope` — echoes the input.
              - `venv_path` — absolute path to the venv root, or empty
                string if no venv applies (agent scope, no venv).
              - `python` — absolute path to the venv's python
                executable.
              - `pip` — absolute path to the venv's pip executable.
              - `python_version` — `"X.Y.Z"`, or empty if it couldn't
                be determined.
              - `exists_before_call` — True iff the venv existed
                before this call. False means this call just created
                it (workspace scope only).
              - `note` — empty on the happy path; carries an inline
                explanation when something atypical happened
                (e.g. agent scope but pyagent isn't in a venv).

            On a structural failure (venv creation blew up), returns a
            `<error: …>` marker string instead of JSON.
        """
        scope = (scope or "workspace").strip().lower()
        if scope not in ("workspace", "agent"):
            return (
                f"<error: scope must be 'workspace' or 'agent', got {scope!r}>"
            )

        if scope == "workspace":
            try:
                venv_path, created = venv_mod.ensure(workspace)
            except RuntimeError as e:
                return f"<error: workspace venv setup failed: {e}>"
            python = venv_mod.python_path(venv_path)
            pip = venv_mod.pip_path(venv_path)
            return json.dumps(
                {
                    "scope": "workspace",
                    "venv_path": str(venv_path),
                    "python": str(python),
                    "pip": str(pip),
                    "python_version": _python_version_for(python),
                    "exists_before_call": not created,
                    "note": "",
                }
            )

        # scope == "agent": the venv pyagent is running under, if any.
        # `sys.prefix != sys.base_prefix` is the canonical "am I in a
        # venv" check; if not, we report honestly without creating.
        in_venv = sys.prefix != sys.base_prefix
        if not in_venv:
            return json.dumps(
                {
                    "scope": "agent",
                    "venv_path": "",
                    "python": sys.executable,
                    "pip": "",
                    "python_version": ".".join(
                        map(str, sys.version_info[:3])
                    ),
                    "exists_before_call": False,
                    "note": (
                        "pyagent is not running in a venv; "
                        "installing into this interpreter would "
                        "modify the system/user Python — refuse "
                        "and surface this to the user instead."
                    ),
                }
            )

        agent_venv = Path(sys.prefix)
        # Use the canonical helpers so Linux/macOS/Windows all agree
        # on bin/Scripts naming, even though we already know
        # `sys.executable`.
        python = venv_mod.python_path(agent_venv)
        pip = venv_mod.pip_path(agent_venv)
        # `pip` may not be present in some minimal venvs — surface
        # that as a note rather than a hard error.
        note = ""
        if not pip.exists():
            note = (
                f"venv at {agent_venv} has no pip at {pip}; bootstrap "
                f"with `{python} -m ensurepip` before installing."
            )
        return json.dumps(
            {
                "scope": "agent",
                "venv_path": str(agent_venv.resolve()),
                "python": sys.executable,
                "pip": str(pip) if pip.exists() else "",
                "python_version": ".".join(map(str, sys.version_info[:3])),
                "exists_before_call": True,
                "note": note,
            }
        )

    return python_env
