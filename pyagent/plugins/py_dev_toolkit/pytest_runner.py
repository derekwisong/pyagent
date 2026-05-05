"""Wrap pytest to return a structured pass/fail summary.

Uses `pytest-json-report` for a reliable structured envelope rather
than parsing pytest's text output (which shifts shape between
versions, plugins, and tracebacks). When the json-report plugin
isn't installed, surfaces a clean error pointing the caller at
`pip install pytest-json-report` (via the workspace venv obtained
through `python_env`) rather than degrading silently.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pyagent import permissions

# pytest can take a while; longer timeout than lint/typecheck. The
# cap is a circuit breaker for hung tests, not a per-test budget.
_TIMEOUT_S = 600

# How many failure / error tracebacks to embed in the summary. More
# than this is hard to read at the agent level — the caller should
# narrow with `k=` or look at attachments instead.
_MAX_FAILURES_SHOWN = 10


def run(
    target: str = ".",
    k: str | None = None,
    fail_fast: bool = False,
    extra_args: list[str] | None = None,
) -> str:
    """Run pytest with structured output.

    Args:
        target: File, directory, or pytest nodeid (e.g.
            `tests/foo.py::TestX::test_y`). Defaults to ".".
        k: Optional `-k EXPR` filter. Same expression syntax as
            pytest's command line.
        fail_fast: If True, pass `-x` so pytest stops on first
            failure. Useful when triaging a single regression.
        extra_args: Optional extra pytest flags (e.g.
            `["--no-cov"]`). Use sparingly — most tuning is better
            done via the project's `pyproject.toml` /
            `pytest.ini`.

    Returns:
        Summary line `pytest <target>: P passed, F failed, S skipped,
        E errors (D.DDs)` followed by failure / error blocks (test
        id + short message + last traceback line). Errors come back
        inline as `<error: ...>`.
    """
    if target is None or not str(target).strip():
        target = "."
    pytest_bin = shutil.which("pytest")
    if not pytest_bin:
        return (
            "<error: pytest is not on PATH; call `python_env` to get "
            "the workspace venv's pip path, then `execute` "
            "`<pip> install pytest pytest-json-report` and retry>"
        )

    # Always gate the target with `require_access`, even if it
    # doesn't exist on disk yet. A non-existent out-of-workspace
    # path (typo, wrong relative path, or deliberately escapist
    # `target="../../../etc/test_x.py"`) still causes pytest to be
    # invoked, and pytest's collection phase walks parent
    # directories for `conftest.py` / `pyproject.toml` — so the
    # right time to prompt is *before* invocation, regardless of
    # whether the named file exists.
    #
    # `require_access` is a no-op for paths that resolve inside the
    # workspace, so the common case (relative paths, default ".")
    # is silent.
    target_path = Path(target.split("::", 1)[0])
    if not permissions.require_access(target_path):
        return f"<error: access denied to {target}>"

    # `mkstemp` over `NamedTemporaryFile(delete=False)`: on Windows
    # the latter can keep handles open across context-manager exit
    # and races the subprocess that wants to write to the same path.
    # `mkstemp` returns an fd we close immediately, leaving only the
    # path for the subprocess.
    fd, report_name = tempfile.mkstemp(suffix=".json", prefix="pytest_report_")
    os.close(fd)
    report_path = Path(report_name)

    try:
        cmd = [
            pytest_bin,
            "--json-report",
            f"--json-report-file={report_path}",
            "--no-header",
            "-q",
        ]
        if k:
            cmd += ["-k", k]
        if fail_fast:
            cmd.append("-x")
        if extra_args:
            cmd.extend(str(a) for a in extra_args)
        cmd.append(target)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return (
                f"<error: pytest timed out after {_TIMEOUT_S}s on {target}>"
            )

        # If the json-report plugin isn't loaded, pytest prints a
        # clear error and exits 4 (usage error). Detect that and
        # point the caller at the install step rather than handing
        # back an opaque exit code.
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "unrecognized arguments: --json-report" in combined:
            return (
                "<error: pytest-json-report plugin missing; call "
                "`python_env` for the venv's pip, then `execute` "
                "`<pip> install pytest-json-report` and retry>"
            )

        try:
            data = json.loads(report_path.read_text())
        except (OSError, json.JSONDecodeError):
            err = (proc.stderr or "").strip() or proc.stdout[:300]
            return (
                f"<error: pytest produced no JSON report "
                f"(exit {proc.returncode}): {err}>"
            )

        return _format_report(data, target)
    finally:
        try:
            report_path.unlink()
        except FileNotFoundError:
            pass


def _format_report(data: dict, target: str) -> str:
    summary = data.get("summary") or {}
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    skipped = summary.get("skipped", 0)
    # pytest-json-report uses `error` (singular) in current versions;
    # the `errors` fallback is belt-and-suspenders for a hypothetical
    # schema rename. If/when we pin a minimum json-report version we
    # can drop one.
    errors = summary.get("error", 0) + summary.get("errors", 0)
    duration = data.get("duration") or 0.0

    parts = [f"{passed} passed"]
    if failed:
        parts.append(f"{failed} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if errors:
        parts.append(f"{errors} errors")
    head = (
        f"pytest {target}: {', '.join(parts)} ({duration:.2f}s)"
    )

    failure_blocks: list[str] = []
    error_blocks: list[str] = []
    for t in data.get("tests") or []:
        outcome = t.get("outcome")
        if outcome not in ("failed", "error"):
            continue
        nodeid = t.get("nodeid", "?")
        # Failures live in the "call" phase; errors usually surface
        # in "setup" (fixture errors) or "teardown".
        for phase in ("call", "setup", "teardown"):
            stage = t.get(phase) or {}
            if stage.get("outcome") == outcome:
                msg = (stage.get("longrepr") or "").strip()
                # Last non-empty traceback line is usually the most
                # informative — show that plus the test id.
                tail = ""
                for ln in reversed(msg.splitlines()):
                    if ln.strip():
                        tail = ln.strip()
                        break
                bucket = (
                    failure_blocks if outcome == "failed" else error_blocks
                )
                bucket.append(
                    f"- {nodeid} ({phase})\n    {tail}"
                )
                break

    body_parts: list[str] = []
    if failure_blocks:
        body_parts.append(
            "failures:\n" + "\n".join(failure_blocks[:_MAX_FAILURES_SHOWN])
        )
        if len(failure_blocks) > _MAX_FAILURES_SHOWN:
            body_parts.append(
                f"… {len(failure_blocks) - _MAX_FAILURES_SHOWN} more failures "
                f"(narrow with k=... to triage individually)"
            )
    if error_blocks:
        body_parts.append(
            "errors:\n" + "\n".join(error_blocks[:_MAX_FAILURES_SHOWN])
        )

    if body_parts:
        return head + "\n\n" + "\n\n".join(body_parts)
    return head
