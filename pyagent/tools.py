"""Built-in tools for the agent."""

import os
import re
import signal
import subprocess
import threading
from pathlib import Path

import requests

from pyagent import paths, permissions
from pyagent.session import Attachment

# Track in-flight execute() shell subprocesses so the cancel pathway
# can kill them on Esc. Within a single agent process the tool loop
# runs serially (so this list is usually 0 or 1), but a list keeps it
# safe if anything ever runs an `execute` from a worker thread.
_ACTIVE_EXEC_PROCS: list[subprocess.Popen] = []
_ACTIVE_EXEC_LOCK = threading.Lock()


def kill_active() -> int:
    """SIGKILL every in-flight execute() shell subprocess group.

    Called by the cancel pathway when Esc is pressed during a long
    shell command. The foreground `execute()` call's `communicate()`
    sees the proc exit and returns whatever output was buffered, so
    the tool reports a normal-looking result (exit_code: -9) and the
    agent loop reaches its next safe-point cancel check immediately.

    Returns the number of process groups signalled.
    """
    killed = 0
    with _ACTIVE_EXEC_LOCK:
        for proc in list(_ACTIVE_EXEC_PROCS):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                # Already exited between the lock and the kill.
                pass
    return killed


def _denied(path: str) -> str:
    return f"<permission denied (outside workspace): {path}>"

_LEDGERS: dict[str, str] = {
    "USER": "USER.md",
    "MEMORY": "MEMORY.md",
}


def read_ledger(name: str) -> str:
    """Read one of the agent's ledgers.

    Ledgers are the agent's persistent notebooks — `USER` (notes about
    the person being helped) and `MEMORY` (long-term memorable facts).
    Their on-disk locations are resolved automatically; do not use
    `read_file` for these.

    Args:
        name: Ledger to read. One of: "USER", "MEMORY".

    Returns:
        The ledger's contents, an empty string if it has not been
        written yet, or an error marker if `name` is not a known
        ledger.
    """
    key = name.upper()
    if key not in _LEDGERS:
        valid = ", ".join(sorted(_LEDGERS))
        return f"<unknown ledger: {name!r}; valid: {valid}>"
    filename = _LEDGERS[key]
    target = paths.resolve(filename, seed=filename)
    if not permissions.require_access(target):
        return _denied(str(target))
    if not target.exists():
        return ""
    return target.read_text()


def write_ledger(name: str, content: str) -> str:
    """Overwrite one of the agent's ledgers with new content.

    Ledgers are the agent's persistent notebooks — `USER` (notes about
    the person being helped) and `MEMORY` (long-term memorable facts).
    Their on-disk locations are resolved automatically; do not use
    `write_file` for these. Writes overwrite; if you only want to add,
    `read_ledger` first, edit, then `write_ledger`.

    Args:
        name: Ledger to write. One of: "USER", "MEMORY".
        content: Full new content of the ledger.

    Returns:
        Confirmation message with the resolved path and byte count, or
        an error marker if `name` is not a known ledger.
    """
    key = name.upper()
    if key not in _LEDGERS:
        valid = ", ".join(sorted(_LEDGERS))
        return f"<unknown ledger: {name!r}; valid: {valid}>"
    filename = _LEDGERS[key]
    target = paths.resolve(filename, seed=filename)
    if not permissions.require_access(target):
        return _denied(str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Wrote {len(content)} bytes to {target}"


def read_file(
    path: str, start: int = 1, end: int | None = None
) -> "str | Attachment":
    """Read a file and return its contents.

    For text files, returns the requested lines as a string. For binary
    files, returns an Attachment whose bytes are saved to the session's
    attachments dir; the conversation gets a short reference (path,
    size) instead of raw bytes.

    Args:
        path: Path to the file to read.
        start: First line to return (1-indexed, inclusive). Text only.
            Defaults to 1.
        end: Last line to return (1-indexed, inclusive). Text only.
            Defaults to end of file.

    Returns:
        Text file contents (possibly truncated above 2000 lines), or an
        Attachment carrying the raw bytes for binary files. Predictable
        failures (missing path, permission denied, etc.) come back as a
        leading `<...>` marker string.
    """
    p = Path(path)
    if not permissions.require_access(p):
        return _denied(path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"<file not found: {path}>"
    except IsADirectoryError:
        return f"<is a directory, not a file: {path}>"
    except PermissionError:
        return f"<permission denied: {path}>"
    except UnicodeDecodeError:
        try:
            data = p.read_bytes()
        except OSError as e:
            return f"<could not read binary file {path}: {e}>"
        suffix = p.suffix or ".bin"
        preview = (
            f"binary file: {path} ({len(data)} bytes, {suffix} format) — "
            "saved as an attachment; use a binary-aware tool "
            "(`file`, `hexdump`, an image viewer, etc.) to inspect it."
        )
        return Attachment(content=data, preview=preview, suffix=suffix)

    lines = text.splitlines(keepends=True)
    total = len(lines)
    s = max(start, 1) - 1

    if s >= total:
        return f"<start={start} is past end of file ({total} lines)>"
    if end is not None and end < start:
        return f"<end={end} is before start={start}>"

    if end is None:
        cap = 2000
        if total - s > cap:
            body = "".join(lines[s : s + cap])
            return (
                f"{body}"
                f"... (truncated: file has {total} lines, "
                f"use start/end to read more)"
            )
        return "".join(lines[s:])

    return "".join(lines[s : min(end, total)])


def write_file(path: str, content: str) -> str:
    """Write content to a file, overwriting any existing file.

    Args:
        path: Path to the file to write.
        content: Content to write.

    Returns:
        Confirmation message with the number of bytes written.
    """
    if not permissions.require_access(path):
        return _denied(path)
    try:
        Path(path).write_text(content)
    except FileNotFoundError:
        return f"<parent directory does not exist: {path}>"
    except IsADirectoryError:
        return f"<is a directory, not a file: {path}>"
    except PermissionError:
        return f"<permission denied: {path}>"
    return f"Wrote {len(content)} bytes to {path}"


def list_directory(path: str) -> list[str]:
    """List the entries in a directory.

    Args:
        path: Directory to list.

    Returns:
        Sorted list of entry names. Directories are suffixed with "/".
        On failure, returns a single-element list containing an error
        marker (e.g. "<path not found: ...>").
    """
    if not permissions.require_access(path):
        return [_denied(path)]
    root = Path(path)
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return [f"<path not found: {path}>"]
    except NotADirectoryError:
        return [f"<not a directory: {path}>"]
    except PermissionError:
        return [f"<permission denied: {path}>"]
    return sorted(f"{e.name}/" if e.is_dir() else e.name for e in entries)


def grep(pattern: str, path: str) -> list[str]:
    """Search for a regex pattern in a file or directory tree.

    Searches recursively if `path` is a directory. Files that cannot be
    decoded as UTF-8 text are skipped.

    Args:
        pattern: Regex pattern to search for.
        path: File or directory to search.

    Returns:
        List of matches formatted as "path:lineno:line".
    """
    if not permissions.require_access(path):
        return [_denied(path)]
    regex = re.compile(pattern)
    root = Path(path)
    if not root.exists():
        return [f"<path not found: {path}>"]
    candidates = [root] if root.is_file() else root.rglob("*")
    results: list[str] = []
    for f in candidates:
        if not f.is_file():
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{f}:{i}:{line}")
    return results


# Patterns that should never run unattended. This is a speed bump
# against accidents, not a sandbox — a determined model can dodge any
# regex with variable expansion, escapes, or base64. Real safety lives
# in the human-in-the-loop and OS-level isolation.
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"--no-preserve-root", "rm bypassing root protection"),
    (
        r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)[^|;&]*\s+"
        r"(?:/|/\*|~|\$HOME"
        r"|/(?:etc|usr|bin|boot|var|home|sbin|lib|opt|root|sys|proc|dev)/?\*?)"
        r"(?:\s|$|[;&|])",
        "recursive rm against root or a top-level system directory",
    ),
    (r"\bdd\b[^|;&]*\bof=/dev/(?:sd|nvme|hd|vd|xvd)", "dd writing to a block device"),
    (r"\bmkfs(?:\.\w+)?\s+/dev/", "mkfs against a device"),
    (r">\s*/dev/(?:sd|nvme|hd|vd|xvd)", "redirect to a block device"),
    (r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (
        r"\b(?:curl|wget)\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b",
        "piping remote script to a shell",
    ),
    (
        r"\bgit\s+push\b[^;&|]*\s(?:--force(?:-with-lease)?|-f)\s"
        r"[^;&|]*\b(?:main|master)\b",
        "force push to main/master",
    ),
    (r"\bchmod\s+-R\s+[0-7]{3,4}\s+/(?:\s|$)", "recursive chmod on /"),
    (
        r"(?:>\s*\S*|\b(?:rm|mv|cp|tee|sed\s+-i\S*)\s+[^|;&]*)"
        r"\.auto_installed\b",
        "tampering with the skills auto-install marker",
    ),
]


def _safety_check(command: str) -> str | None:
    """Return a short reason string if `command` matches a blocked
    pattern, else None."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return reason
    return None


def execute(command: str) -> str:
    """Run a shell command and return its output.

    Args:
        command: Shell command to run.

    Returns:
        Combined report of exit code, stdout, and stderr.
    """
    blocked = _safety_check(command)
    if blocked:
        return (
            f"<refused: matches dangerous pattern ({blocked}); "
            f"ask the human to run it manually if intended>"
        )
    # Run the shell in its own process group so a timeout takes the whole
    # tree (including grandchildren) rather than orphaning them. stdin is
    # closed so subprocesses can't accidentally consume the parent's
    # raw-mode stdin or hang waiting for input.
    proc = subprocess.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    with _ACTIVE_EXEC_LOCK:
        _ACTIVE_EXEC_PROCS.append(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=60)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return f"<command timed out after 60s: {command}>"
    finally:
        with _ACTIVE_EXEC_LOCK:
            try:
                _ACTIVE_EXEC_PROCS.remove(proc)
            except ValueError:
                pass
    if stdout and not stdout.endswith("\n"):
        stdout += "\n"
    return (
        f"exit_code: {returncode}\n"
        f"stdout:\n{stdout}"
        f"stderr:\n{stderr}"
    )


_FETCH_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_url(url: str) -> str:
    """Fetch a URL via HTTP GET and return the response body as text.

    Args:
        url: URL to fetch.

    Returns:
        Status code followed by the response body. Network failures are
        reported as an error string rather than raised.
    """
    try:
        response = requests.get(
            url, headers={"User-Agent": _FETCH_UA}, timeout=30
        )
    except requests.RequestException as e:
        return f"<request failed: {e}>"
    return f"status: {response.status_code}\n{response.text}"
