"""Built-in tools for the agent."""

import os
import re
import signal
import subprocess
import threading
from pathlib import Path

import requests

from pyagent import permissions
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


# read_ledger / write_ledger moved to the bundled memory-markdown
# plugin (pyagent/plugins/memory_markdown/) in stage 2 of the plugin
# migration. They are no longer core tools — disabling the plugin
# removes them entirely.


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
            Matches the line numbers `grep` emits and the editor's
            line gutter. Defaults to 1.
        end: Last line to return (1-indexed, inclusive). Text only.
            Defaults to end of file.

    Returns:
        Text file contents (possibly truncated above 2000 lines), or an
        Attachment carrying the raw bytes for binary files. Predictable
        failures (missing path, permission denied, etc.) come back as a
        leading `<...>` marker string.
    """
    # Models occasionally emit numeric tool args as strings ("50" instead
    # of 50) even when the JSON schema declares int. Coerce defensively
    # so the tool returns an actionable error instead of crashing the
    # turn — surfaced live during the pyagent_self_audit bench run.
    try:
        start = int(start)
    except (TypeError, ValueError):
        return f"<error: start must be an integer, got {start!r}>"
    if end is not None:
        try:
            end = int(end)
        except (TypeError, ValueError):
            return f"<error: end must be an integer or null, got {end!r}>"

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


def write_file(path: str, content: str, append: bool = False) -> str:
    """Write content to a file.

    Default behavior overwrites the file. With `append=True`, content is
    appended to the end (file is created if missing). For small targeted
    modifications, prefer `edit_file` over re-emitting the whole file —
    `edit_file` only sends the diff, so the prior version doesn't ride
    every subsequent turn.

    Append mode is the recovery path when a single artifact is too
    large to emit in one tool call: write the first chunk normally,
    then follow up with `append=True` calls. Do not fall back to shell
    heredocs via `execute` for this — those embed the whole file in a
    shell string that lives in the conversation forever.

    Args:
        path: Path to the file to write.
        content: Content to write (or append).
        append: If True, append to the existing file instead of
            overwriting. Defaults to False.

    Returns:
        Confirmation message with the resolved path and byte count, or
        an error marker if the parent directory is missing, the path
        names a directory, or permission is denied.
    """
    if not permissions.require_access(path):
        return _denied(path)
    try:
        if append:
            with Path(path).open("a") as f:
                f.write(content)
        else:
            Path(path).write_text(content)
    except FileNotFoundError:
        return f"<parent directory does not exist: {path}>"
    except IsADirectoryError:
        return f"<is a directory, not a file: {path}>"
    except PermissionError:
        return f"<permission denied: {path}>"
    verb = "Appended" if append else "Wrote"
    return f"{verb} {len(content)} bytes to {path}"


def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Replace text in a file by exact-string match.

    Cheaper than re-emitting the whole file with `write_file`: the
    diff is the only thing that enters the conversation. Reach for
    this whenever you'd otherwise rewrite an existing file to change
    a few lines.

    `old_string` must match exactly once. If it appears multiple
    times, expand it with surrounding context until it's unique — or
    pass `replace_all=True` for renames and similar bulk swaps.

    Args:
        path: Path to the file to edit.
        old_string: Exact text to find. Must be unique unless
            `replace_all=True`. Multi-line strings are supported.
        new_string: Replacement text.
        replace_all: If True, replace every occurrence of
            `old_string`. Defaults to False (single-occurrence
            uniqueness required).

    Returns:
        Confirmation message naming the path and how many occurrences
        were replaced (and the line number for single replacements),
        or an error marker if the file is missing, the match is
        ambiguous, or the match is not found.
    """
    if not permissions.require_access(path):
        return _denied(path)
    if not old_string:
        return "<error: old_string is empty>"
    if old_string == new_string:
        return "<error: old_string and new_string are identical>"
    p = Path(path)
    try:
        text = p.read_text()
    except FileNotFoundError:
        return f"<file not found: {path}>"
    except IsADirectoryError:
        return f"<is a directory, not a file: {path}>"
    except PermissionError:
        return f"<permission denied: {path}>"
    except UnicodeDecodeError:
        return f"<cannot decode {path} as UTF-8>"

    count = text.count(old_string)
    if count == 0:
        return f"<error: old_string not found in {path}>"
    if count > 1 and not replace_all:
        return (
            f"<error: old_string matches {count} times in {path}; "
            f"expand it with surrounding context to make it unique, "
            f"or pass replace_all=True>"
        )

    if replace_all:
        new_text = text.replace(old_string, new_string)
        success = (
            f"Edited {path}: replaced {count} "
            f"{'occurrence' if count == 1 else 'occurrences'}"
        )
    else:
        idx = text.find(old_string)
        line_no = text[:idx].count("\n") + 1
        new_text = text[:idx] + new_string + text[idx + len(old_string):]
        success = f"Edited {path}: replaced 1 occurrence at line {line_no}"

    try:
        p.write_text(new_text)
    except FileNotFoundError:
        return f"<file not found: {path}>"
    except IsADirectoryError:
        return f"<is a directory, not a file: {path}>"
    except PermissionError:
        return f"<permission denied: {path}>"
    return success


def list_directory(path: str) -> list[str]:
    """List the entries in a directory.

    Reach for this when you don't yet know a directory's layout — it's
    cheaper than `grep`-on-a-tree when you're just orienting. Use it
    before `read_file` or `grep` if the file paths aren't already known.
    Directories come back with a trailing `/` so they're visually
    distinct from regular files.

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

    First reach for any "where does X appear?" question — cheaper than
    reading whole files to skim. Searches recursively if `path` is a
    directory. Files that cannot be decoded as UTF-8 text are skipped.
    Output line numbers match `read_file`'s `start`/`end` so you can
    pipe a hit into a targeted read.

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

    Hard 60-second timeout per call; commands that exceed it are killed
    (whole process group) and return `<command timed out after 60s: ...>`.
    Use this for git, scripts, builds, tests, one-off shell utilities,
    and HTTP requests that need more than `fetch_url`'s GET-only support.

    Runs with the user's privileges. Destructive or irreversible
    operations (`rm -rf`, force pushes, dropping data, killing
    processes, mass file moves, anything that touches shared state)
    need explicit user consent *before* invocation, not after. A small
    allowlist of clearly dangerous patterns (recursive rm at root,
    fork bombs, piping curl|sh, force push to main, etc.) is refused
    automatically with `<refused: ...>`; treat that as a speed bump
    against accidents, not a sandbox.

    Args:
        command: Shell command to run.

    Returns:
        Multi-line report — `exit_code: N`, then `stdout:` followed by
        captured stdout, then `stderr:` followed by captured stderr.
        Timeout or refusal come back as a leading `<...>` marker
        instead of the normal report.
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

# Cap the inline markdown so a giant page can't blow the conversation.
# Past this size we truncate the inline body and tell the agent to call
# html_to_md(path, ...) on the saved raw attachment for the full output.
_FETCH_INLINE_MD_CEILING = 8000

# Soft import: when the html-tools plugin is enabled (the default),
# fetch_url uses its conversion as a convenience. When the plugin is
# disabled or its deps are missing, we fall back to raw-attachment-only.
try:
    from pyagent.plugins.html_tools import extraction as _html_extraction
except ImportError:
    _html_extraction = None


def _detect_content_type(headers: dict, body: str) -> tuple[str, bool]:
    """Return (content_type, is_html). content_type is the mime portion
    of the Content-Type header, lowercased; is_html is True for HTML
    responses (used to decide whether markdown conversion applies)."""
    raw = (headers.get("Content-Type") or headers.get("content-type") or "")
    ctype = raw.split(";", 1)[0].strip().lower()
    if not ctype:
        # Cheap content sniff for hosts that don't set Content-Type.
        head = body[:512].lstrip().lower()
        if head.startswith("<!doctype html") or head.startswith("<html"):
            ctype = "text/html"
        elif head.startswith(("{", "[")):
            ctype = "application/json"
        else:
            ctype = "text/plain"
    is_html = "html" in ctype
    return ctype, is_html


def _suffix_for_content_type(ctype: str) -> str:
    if "html" in ctype:
        return ".html"
    if "json" in ctype:
        return ".json"
    if "xml" in ctype:
        return ".xml"
    return ".txt"


def fetch_url(
    url: str,
    format: str = "md",
    main_content: bool = True,
) -> "str | Attachment":
    """Fetch a URL via HTTP GET; save the raw response and return a
    convenience preview.

    The raw response body is *always* saved to a session attachment.
    The returned tool result names that path so you can follow up with
    `html_to_md`, `html_select`, `grep`, or `read_file` against the
    saved file — no need to re-fetch.

    GET-only. Non-2xx responses still save and report normally; the
    HTTP status appears in the result. For POST, custom headers, auth,
    or anything beyond a plain GET, drop into `execute` with `curl`.

    Args:
        url: URL to fetch.
        format: Output format. `"md"` (default) converts HTML responses
            to markdown and includes it inline alongside the saved-path
            stub — one tool call covers article-body extraction.
            `"void"` skips conversion and returns only the path stub
            with no inline body — use when you'll interrogate the page
            with `html_select` / `grep` and don't want to pay tokens
            for a markdown preview you won't read, or when fetching
            multiple URLs to triage cheaply.
        main_content: When `format="md"` and the response is HTML, run
            a readability-style reduction first (drop nav/aside/footer
            and prefer `<main>` / `<article>`). Default `True` matches
            the news/blog/article majority. Set False for reference
            pages (Wikipedia, docs) where the whole document is the
            content.

    Returns:
        An attachment-style stub naming the saved path, the response
        status and content type, and — when applicable — the converted
        markdown inline. Network failures (DNS, connection refused,
        timeout) come back as `<request failed: ...>` instead.
    """
    try:
        response = requests.get(
            url, headers={"User-Agent": _FETCH_UA}, timeout=30
        )
    except requests.RequestException as e:
        return f"<request failed: {e}>"

    body = response.text
    ctype, is_html = _detect_content_type(dict(response.headers), body)
    suffix = _suffix_for_content_type(ctype)
    size = len(body)

    header_lines = [
        f"Fetched {url} (status {response.status_code}, "
        f"{size} chars, {ctype}).",
    ]

    if format == "void":
        header_lines.append(
            "No content returned (format=\"void\"). Use `html_to_md`, "
            "`html_select`, `grep`, or `read_file` on the saved path "
            "to interrogate."
        )
        preview = "\n".join(header_lines)
        return Attachment(content=body, preview=preview, suffix=suffix)

    if not is_html or _html_extraction is None:
        if not is_html:
            header_lines.append(
                f"Non-HTML response. Use `read_file` / `grep` on the "
                f"saved path to extract."
            )
        else:
            header_lines.append(
                "html-tools plugin not available; markdown conversion "
                "skipped. Use `read_file` / `grep` on the saved path."
            )
        preview = "\n".join(header_lines)
        return Attachment(content=body, preview=preview, suffix=suffix)

    try:
        md = _html_extraction.html_to_markdown(
            body, main_content=main_content
        )
    except Exception as e:
        header_lines.append(
            f"Markdown conversion failed ({type(e).__name__}: {e}). "
            f"Use `html_to_md` directly on the saved path or fall back "
            f"to `read_file` / `grep`."
        )
        preview = "\n".join(header_lines)
        return Attachment(content=body, preview=preview, suffix=suffix)

    md_size = len(md)
    truncated = md_size > _FETCH_INLINE_MD_CEILING
    if truncated:
        md_inline = (
            md[:_FETCH_INLINE_MD_CEILING]
            + "\n\n[markdown truncated; call "
            "`html_to_md(<saved path>, main_content="
            f"{main_content})` for the full output.]"
        )
    else:
        md_inline = md

    label = "main-content markdown" if main_content else "full-document markdown"
    header_lines.append(
        f"{label} ({md_size} chars{', truncated inline' if truncated else ''}). "
        f"For CSS extraction or raw search, use `html_select` / "
        f"`grep` / `read_file` on the saved path."
    )
    preview = "\n".join(header_lines) + "\n\n" + md_inline
    return Attachment(content=body, preview=preview, suffix=suffix)
