"""Wrap mypy / pyright to return structured type-check findings.

mypy: tries `mypy -O json` first (mypy ≥ 1.11 emits JSONL); falls
back to text parsing on older mypy. Both paths feed the same output
shape and both filter `severity == "note"` — notes are context the
caller may want eventually but they aren't *findings*, and counting
them inflates the summary ("5 findings: 1 error, 4 notes" reads as
five problems when there's only one).

pyright: uses native `--outputjson`. Same output shape; "information"
and "hint" diagnostics are filtered for the same reason.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from pyagent import permissions
from pyagent.plugins.py_dev_toolkit._pathutil import shorten as _shorten

_TIMEOUT_S = 120  # typecheckers are slower than linters; bump from 60.

# Text-format mypy line: `path:line:col: severity: message [code]`.
# `(?P<file>.+?)` is non-greedy so it accepts paths containing
# colons — Windows drive letters (`C:\foo\bar.py:3:1: error: …`)
# being the case that bit us. Earlier `[^:]+` silently dropped
# every error on Windows, returning a false-clean result.
#
# Edge case the non-greedy match doesn't fully cover: a POSIX path
# with a single colon followed by a digit (e.g. `dir:9file.py`)
# could in principle confuse the engine. In practice it matches
# correctly because the trailing `:\s+(error|note|warning):\s+`
# anchor requires a real severity token after the second colon —
# the engine backtracks until the file portion is the right shape.
# Documented because the algorithmic guarantee isn't obvious from
# the regex alone.
_MYPY_TEXT_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s+"
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
        Notes (mypy `note:` lines, pyright "information"/"hint"
        diagnostics) are excluded from both the count and the bullet
        list — they're context, not findings.
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
            f"<error: {tool} is not on PATH; call `python_env` to "
            f"get the workspace venv's pip path, then `execute` "
            f"`<pip> install {tool}` and retry>"
        )

    if tool == "mypy":
        return _run_mypy(binary, target)
    return _run_pyright(binary, target)


def _run_mypy(binary: str, target: Path) -> str:
    """Run mypy, preferring JSON output. Falls back to text parsing
    on older mypy that doesn't recognize `-O json`."""
    json_attempt = _try_mypy_json(binary, target)
    if json_attempt is not None:
        findings, error = json_attempt
        if error:
            return error
    else:
        text_attempt = _run_mypy_text(binary, target)
        if isinstance(text_attempt, str):
            return text_attempt
        findings = text_attempt

    if not findings:
        return f"mypy: clean (no findings on {target})"
    return _format("mypy", findings, str(target))


def _try_mypy_json(
    binary: str, target: Path
) -> tuple[list[dict], str | None] | None:
    """Try `mypy -O json`. Returns:
      - `(findings, None)` on success.
      - `(_, error_string)` when mypy ran but failed.
      - `None` when the JSON flag isn't recognized (older mypy);
        caller should fall back to text parsing.
    """
    try:
        proc = subprocess.run(
            [binary, "-O", "json", "--no-color-output", str(target)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return [], f"<error: mypy timed out after {_TIMEOUT_S}s>"

    # Robustly detect "JSON flag not recognized" by inspecting the
    # *first non-empty stdout line* rather than substring-matching
    # mypy's error wording. If JSON is producing output, the first
    # non-empty line is a JSON object (`{...}`); anything else means
    # mypy printed a usage error or a localized message and we
    # should fall through to text parsing instead of trusting what
    # we got. This survives mypy reword / locale changes.
    first = next(
        (ln for ln in (proc.stdout or "").splitlines() if ln.strip()),
        "",
    ).lstrip()
    if first and not first.startswith("{"):
        return None  # older mypy — fall back to text parsing

    # mypy exits 1 when it finds errors — that's normal. Treat
    # exit > 1 as failure only if no JSON output came through; some
    # configurations print partial results plus a non-zero status
    # (plugin failures, etc.), and we'd rather surface what we got
    # than swallow it.
    if proc.returncode > 1 and not first:
        err = (proc.stderr or "").strip() or "(no stderr)"
        return [], f"<error: mypy failed (exit {proc.returncode}): {err}>"

    findings = parse_mypy_json(proc.stdout or "")
    return findings, None


def parse_mypy_json(text: str) -> list[dict]:
    """Parse mypy's `-O json` output (JSONL — one JSON object per
    line). Skips `severity == "note"` for the same reason as the
    text parser. Lines that aren't JSON (mypy's footer, progress
    output) are silently skipped.

    Exposed at module scope (no `_` prefix) so tests can exercise
    the parser without spawning mypy.
    """
    findings: list[dict] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            d = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if d.get("severity") == "note":
            continue
        findings.append(
            {
                "filename": d.get("file", "?"),
                "line": int(d.get("line") or 0),
                "col": int(d.get("column") or 0),
                "severity": d.get("severity", "error"),
                "code": d.get("code") or "",
                "message": d.get("message", ""),
            }
        )
    return findings


def _run_mypy_text(binary: str, target: Path) -> list[dict] | str:
    """Fallback for mypy < 1.11. Returns a list on success or an
    error string on subprocess failure."""
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

    if proc.returncode > 1:
        err = (proc.stderr or "").strip() or proc.stdout[:200]
        return f"<error: mypy failed (exit {proc.returncode}): {err}>"

    return parse_mypy_text(proc.stdout or "")


def parse_mypy_text(text: str) -> list[dict]:
    """Parse mypy's text output into the canonical finding shape.

    Skips `note:` lines (they're context for an adjacent error,
    not a finding in their own right; counting them inflates the
    summary and confuses the caller). Lines that don't match the
    expected shape are also skipped — usually mypy's "Found N
    errors" footer or progress output.

    Exposed at module scope (no `_` prefix) so tests can exercise
    the parser without spawning mypy.
    """
    findings: list[dict] = []
    for raw in text.splitlines():
        m = _MYPY_TEXT_LINE.match(raw)
        if m is None:
            continue
        sev = m.group("sev")
        if sev == "note":
            continue
        findings.append(
            {
                "filename": m.group("file"),
                "line": int(m.group("line")),
                "col": int(m.group("col")),
                "severity": sev,
                "code": m.group("code") or "",
                "message": m.group("msg"),
            }
        )
    return findings


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
        # "information" / "hint" are pyright's equivalent of mypy
        # notes — context, not findings. Skip for the same reason.
        if d.get("severity") in ("information", "hint"):
            continue
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
            f"- {_shorten(f['filename'])}:{f['line']}:{f['col']} "
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
