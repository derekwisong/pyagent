"""py-dev-toolkit — bundled plugin: structured Python dev tools.

Tools that either wrap external CLIs (returning structured output the
agent can act on without parsing tea leaves) or surface environment
state the agent needs to compose its own commands:

  - `lint(path, tools=["ruff"])` — ruff findings as a bullet list.
  - `typecheck(path, tool="mypy"|"pyright")` — type errors / warnings.
  - `run_pytest(target, k=None, fail_fast=False)` — pass/fail summary
    plus failure tracebacks via pytest-json-report.
  - `python_env(scope="workspace"|"agent")` — paths + version of the
    workspace's `.venv/` (lazily created) or pyagent's own venv. The
    agent then drives `pip install …` / `python …` itself via
    `execute` using the returned absolute paths.

Why a plugin rather than letting the agent shell out via `execute`:
for the wrappers, the structured envelope is the value — each tool's
text output is fragile (version-dependent, formatter-dependent,
plugin-dependent), and re-emitting one canonical bullet shape moves
the model from "interpret a wall of text" to "act on a list of
findings." For `python_env` the value is bootstrap + introspection:
the agent gets the venv path (creating it on first call), and from
there everything else is the LLM composing pip / python invocations
it already knows fluently.

Each wrapper tool checks its own binary at call time and returns a
clean error if missing — no manifest [requires] gate, so a host
with only ruff installed still benefits from `lint` while
`typecheck` / `run_pytest` surface their own missing-tool errors.
"""

from __future__ import annotations

from pyagent.plugins.py_dev_toolkit import lint as _lint
from pyagent.plugins.py_dev_toolkit import python_env as _python_env
from pyagent.plugins.py_dev_toolkit import pytest_runner as _pytest_runner
from pyagent.plugins.py_dev_toolkit import typecheck as _typecheck

_PYTHON_GUIDANCE = """\
## Python environments

Discover before invoking: `python_env` returns the workspace venv's
`python`, `pip`, and version. First call bootstraps `.venv/`; later
calls just report. Install via `execute("<pip> install ...")` using
the path it returned — not bare `pip`, which lands wherever PATH
points.

Self-improvement: when the code you wrote runs *inside* this pyagent
process (a plugin, a hook), call `python_env(scope="agent")` and
install there — that's the only venv where its imports resolve.

`ModuleNotFoundError` is an environment problem. Get the venv pip
via `python_env`, then install, ask, or diagnose from there. A bare
`pip install X` handed to the user lands wherever PATH points —
usually wrong.
"""


def _render_python_guidance(_ctx) -> str:
    return _PYTHON_GUIDANCE


def register(api):
    api.register_tool("lint", _lint.run, role_only=True)
    api.register_tool("typecheck", _typecheck.run, role_only=True)
    api.register_tool("run_pytest", _pytest_runner.run, role_only=True)
    api.register_tool("python_env", _python_env.make_python_env(api.workspace))
    api.register_prompt_section(
        "python-guidance", _render_python_guidance, volatile=False
    )
