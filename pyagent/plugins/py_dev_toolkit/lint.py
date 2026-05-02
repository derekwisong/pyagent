"""Wrap `ruff check` to return structured lint findings.

Why a wrapper rather than the agent calling `execute("ruff check ...")`:
the JSON output is structured at the source — we extract `file`,
`line`, `col`, `code`, `message`, `severity`, and fixability cleanly
and re-emit a compact bullet list. The agent stops parsing tea
leaves and starts acting on findings.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pyagent import permissions

_TIMEOUT_S = 60


def run(path: str, tools: list[str] | None = None) -> str:
    """Run linters against a Python file or directory.

    Args:
        path: File or directory path. Workspace-relative or absolute.
            Outside-workspace paths trigger a permission prompt via
            the standard pyagent path gate.
        tools: Linter binaries to run. Defaults to `["ruff"]`. Only
            "ruff" is supported in v1; the parameter exists so a
            future caller can opt into additional linters without an
            API break.

    Returns:
        On findings: a one-line summary (`ruff: N findings on PATH
        (E errors, W warnings, K fixable)`) followed by a blank line
        and a `- file:line:col [code] message` bullet per finding,
        with ` — fixable` appended when `--fix` could clear it.
        On a clean run: `ruff: clean (no findings on PATH)`.
        Errors come back inline as `<error: ...>`.
    """
    if not path or not str(path).strip():
        return "<error: path is required>"
    if tools is None:
        tools = ["ruff"]
    unsupported = [t for t in tools if t != "ruff"]
    if unsupported:
        return (
            f"<error: only 'ruff' is supported in v1 "
            f"(got unsupported: {unsupported})>"
        )

    target = Path(path)
    if not target.exists():
        return f"<error: path does not exist: {path}>"
    if not permissions.require_access(target):
        return f"<error: access denied to {path}>"

    binary = shutil.which("ruff")
    if not binary:
        return (
            "<error: ruff is not on PATH; install via "
            "`pip_install ruff` and retry>"
        )

    try:
        proc = subprocess.run(
            [binary, "check", "--output-format", "json", str(target)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"<error: ruff timed out after {_TIMEOUT_S}s>"

    # ruff exits non-zero when findings exist; treat that as success
    # for our purposes. JSON parse failure is the actual error
    # condition (binary crashed, output corrupted).
    out = proc.stdout or ""
    try:
        findings = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        err = (proc.stderr or "").strip() or out[:200]
        return f"<error: ruff returned non-JSON output: {err}>"

    if not findings:
        return f"ruff: clean (no findings on {target})"

    return _format_findings(findings, str(target))


def _format_findings(findings: list[dict], target: str) -> str:
    by_sev: dict[str, int] = {}
    fixable = 0
    lines: list[str] = []
    for f in findings:
        loc = f.get("location") or {}
        line = loc.get("row", "?")
        col = loc.get("column", "?")
        code = f.get("code", "?")
        message = f.get("message", "")
        sev = f.get("severity", "error")
        by_sev[sev] = by_sev.get(sev, 0) + 1
        fix_marker = ""
        if f.get("fix"):
            fixable += 1
            fix_marker = " — fixable"
        file_rel = f.get("filename", "?")
        lines.append(
            f"- {file_rel}:{line}:{col} [{code}] {message}{fix_marker}"
        )

    sev_part = ", ".join(
        f"{n} {sev}{'s' if n != 1 else ''}"
        for sev, n in sorted(by_sev.items())
    )
    fix_part = f", {fixable} fixable" if fixable else ""
    summary = (
        f"ruff: {len(findings)} finding"
        f"{'s' if len(findings) != 1 else ''} on "
        f"{target} ({sev_part}{fix_part})"
    )
    return summary + "\n\n" + "\n".join(lines)
