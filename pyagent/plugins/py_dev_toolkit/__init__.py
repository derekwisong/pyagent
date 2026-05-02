"""py-dev-toolkit — bundled plugin: structured Python dev tools.

Three tools that wrap external CLIs and return structured output the
agent can act on without parsing tea leaves:

  - `lint(path, tools=["ruff"])` — ruff findings as a bullet list.
  - `typecheck(path, tool="mypy"|"pyright")` — type errors / warnings.
  - `run_pytest(target, k=None, fail_fast=False)` — pass/fail summary
    plus failure tracebacks via pytest-json-report.

Why a plugin rather than letting the agent shell out via `execute`:
the structured envelope is the value. Each tool's text output is
fragile (version-dependent, formatter-dependent, plugin-dependent);
parsing JSON / a stable subset of mypy's text and re-emitting one
canonical bullet shape means the calling agent gets `file:line:col
[code] message` per finding regardless of which tool ran. That
moves the model from "interpret a wall of text" to "act on a list
of findings."

Each tool checks its own binary at call time and returns a clean
error if missing — no manifest [requires] gate, so a host with only
ruff installed still benefits from `lint` while `typecheck` /
`run_pytest` surface their own missing-tool errors.
"""

from __future__ import annotations

from pyagent.plugins.py_dev_toolkit import lint as _lint
from pyagent.plugins.py_dev_toolkit import pytest_runner as _pytest_runner
from pyagent.plugins.py_dev_toolkit import typecheck as _typecheck


def register(api):
    api.register_tool("lint", _lint.run)
    api.register_tool("typecheck", _typecheck.run)
    api.register_tool("run_pytest", _pytest_runner.run)
