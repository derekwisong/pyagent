"""Wrap mypy / pyright to return structured type-check findings.

mypy emits text in a stable shape (`file:line:col: severity:
message [code]`) and we parse that. pyright has native JSON
(`--outputjson`) and we use that directly. Both feed the same
output shape so the calling agent sees one format regardless of
which typechecker is on the host.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from pyagent import permissions

_TIMEOUT_S = 120  # typecheckers are slower than linters; bump from 60.

# `path:line:col: severity: message [code]` — mypy with
# --show-column-numbers and default error codes on. The code is the
# bracketed last token; older mypy versions sometimes omit it.
_MYPY_LINE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s+"
    r"(?P<sev>error|note|warning):\s+(?P<msg>.*?)"
    r"(?:\s+\[(?P<code>[\w-]+)\])?\s*$"
)


def run(path: str, tool: str = "mypy") -> str:
    """Type-check a Python file or directory.

    Args:
        path: File or directory path. Workspace-relative or absolute.
        tool: "mypy" (default) or "pyright". Pick whichever the
            project already configures; both feed the same output
            shape so the choice is about ecosystem fit, not parsing.

    Returns:
        On findings: a one-line summary (`<tool>: N findings on PATH
        (E errors, W warnings)`) followed by a blank line and a
        `- file:line:col [code] message` bullet per finding.
        On a clean run: `<tool>: clean (no findings on PATH)`.
        Errors come back inline as `<error: ...>`.
    """
    if not path or not str(path).strip():
        return "<error: path is required>"
    if tool not in ("mypy", "pyright"):
        return (
            f"<error: tool must be 'mypy' or 'pyright', got {tool!r}>"
        )

    target = Path(path)
    if not target.exists():
        return f"<error: path does not exist: {path}>"
    if not permissions.require_access(target):
        return f"<error: access denied to {path}>"

    binary = shutil.which(tool)
    if not binary:
        return (
            f"<error: {tool} is not on PATH; install via "
            f"`pip_install {tool}` and retry>"
        )

    if tool == "mypy":
        return _run_mypy(binary, target)
    return _run_pyright(binary, target)


def _run_mypy(binary: str, target: Path) -> str:
    try:
        proc = subprocess.run(
            [
                binary,
                "--no-error-summary",
                "--show-column-numbers",
                "--no-color-output",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"<error: mypy timed out after {_TIMEOUT_S}s>"

    # mypy exits 1 when it finds errors — that's normal. exit > 1
    # means a real failure (couldn't import, internal error).
    if proc.returncode > 1:
        err = (proc.stderr or "").strip() or proc.stdout[:200]
        return f"<error: mypy failed (exit {proc.returncode}): {err}>"

    findings: list[dict] = []
    for raw in (proc.stdout or "").splitlines():
        m = _MYPY_LINE.match(raw)
        if m is None:
            continue
        findings.append(
            {
                "filename": m.group("file"),
                "line": int(m.group("line")),
                "col": int(m.group("col")),
                "severity": m.group("sev"),
                "code": m.group("code") or "",
                "message": m.group("msg"),
            }
        )

    if not findings:
        return f"mypy: clean (no findings on {target})"
    return _format("mypy", findings, str(target))


def _run_pyright(binary: str, target: Path) -> str:
    try:
        proc = subprocess.run(
            [binary, "--outputjson", str(target)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"<error: pyright timed out after {_TIMEOUT_S}s>"

    out = proc.stdout or ""
    try:
        data = json.loads(out) if out.strip() else {}
    except json.JSONDecodeError:
        err = (proc.stderr or "").strip() or out[:200]
        return f"<error: pyright returned non-JSON output: {err}>"

    diagnostics = data.get("generalDiagnostics") or []
    findings: list[dict] = []
    for d in diagnostics:
        rng = (d.get("range") or {}).get("start") or {}
        findings.append(
            {
                "filename": d.get("file", "?"),
                "line": int(rng.get("line", 0)) + 1,  # pyright is 0-based
                "col": int(rng.get("character", 0)) + 1,
                "severity": d.get("severity", "error"),
                "code": d.get("rule") or "",
                "message": d.get("message", ""),
            }
        )

    if not findings:
        return f"pyright: clean (no findings on {target})"
    return _format("pyright", findings, str(target))


def _format(tool: str, findings: list[dict], target: str) -> str:
    by_sev: dict[str, int] = {}
    lines: list[str] = []
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        code_part = f"[{f['code']}] " if f["code"] else ""
        lines.append(
            f"- {f['filename']}:{f['line']}:{f['col']} "
            f"{code_part}{f['message']}"
        )
    sev_part = ", ".join(
        f"{n} {sev}{'s' if n != 1 else ''}"
        for sev, n in sorted(by_sev.items())
    )
    summary = (
        f"{tool}: {len(findings)} finding"
        f"{'s' if len(findings) != 1 else ''} on {target} ({sev_part})"
    )
    return summary + "\n\n" + "\n".join(lines)
