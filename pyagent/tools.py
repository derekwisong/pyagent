"""Built-in tools for the agent.

## Error-marker contract

Tool results are strings. When a tool wants to signal failure or
refusal *as data* — without raising — it returns a string starting
with the `<` character. By convention the rest of the marker is a
short categorising word followed by a colon, e.g.
``<refused: …>``, ``<unknown sid …>``, ``<no answer from …>``,
``<send failed: …>``. Raised exceptions caught at the dispatch
boundary (``Agent._route_tool``) are also rendered into the same
``<…>`` shape (``Error: <type>: <msg>``).

**Non-error tool results MUST NOT start with ``<``.** Plugins (and
session-audit code) rely on this to detect failures structurally
without a per-tool keyword list. The helper ``is_error_result(s)``
encodes the contract; controlling-hook plugins receive the same
signal as a boolean ``is_error`` argument to the ``after_tool``
hook.
"""

import fnmatch
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from pyagent import permissions
from pyagent.session import Attachment

#: Prefix character that marks an errors-as-data tool result.
ERROR_MARKER_PREFIX = "<"


def is_error_result(content: str) -> bool:
    """Return True iff ``content`` is a tool error/refusal marker.

    Encodes the contract documented in this module's docstring:
    non-error tool results MUST NOT start with ``<``. Used by the
    plugin dispatch loop to give v2 ``after_tool`` hooks a clean
    boolean failure signal so they don't have to sniff for the
    prefix themselves.

    Tolerant of leading whitespace (some tools format with a
    leading newline for readability).
    """
    if not isinstance(content, str):
        return False
    s = content.lstrip()
    return s.startswith(ERROR_MARKER_PREFIX)

# Track in-flight execute() shell subprocesses so the cancel pathway
# can kill them on Esc. Within a single agent process the tool loop
# runs serially (so this list is usually 0 or 1), but a list keeps it
# safe if anything ever runs an `execute` from a worker thread.
_ACTIVE_EXEC_PROCS: list[subprocess.Popen] = []
_ACTIVE_EXEC_LOCK = threading.Lock()

# Per-stream rolling cap for background-process output. When either
# stdout or stderr crosses this size, the oldest 256KB are dropped and
# a `...truncated NN bytes...` notice rides the next read so the agent
# knows the tail is incomplete.
_BG_BUF_CAP = 1024 * 1024
_BG_BUF_DROP = 256 * 1024


@dataclass
class _BackgroundProc:
    """Live state for one `run_background` shell subprocess.

    Both reader threads pump bytes into a single combined `output_buf`
    under `lock`; tools that read or wait take the same lock when
    peeking. A `[stderr]\\n` / `[stdout]\\n` marker is inserted on
    stream transitions so the agent can tell sources apart even
    though exact temporal interleaving across streams isn't preserved.
    `dropped` counts bytes evicted by the rolling cap so a subsequent
    `read_output` can prepend `...truncated NN bytes...`.

    Single-buffer design (vs. one buffer per stream): `since` is then
    a single absolute byte offset into a single sequence. With
    separate buffers, the same `since` applied to two independent
    cursors silently dropped data on the slower stream.
    """

    handle: str
    name: str
    command: str
    proc: subprocess.Popen
    start_time: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    output_buf: bytearray = field(default_factory=bytearray)
    dropped: int = 0
    last_source: str = ""  # "stdout" | "stderr" | "" (initial)
    last_write: float = 0.0
    threads: list[threading.Thread] = field(default_factory=list)


_ACTIVE_BG_PROCS: dict[str, _BackgroundProc] = {}
_ACTIVE_BG_LOCK = threading.Lock()


def kill_active() -> int:
    """SIGKILL every in-flight execute() shell subprocess group.

    Called by the cancel pathway when Esc is pressed during a long
    shell command. The foreground `execute()` call's `communicate()`
    sees the proc exit and returns whatever output was buffered, so
    the tool reports a normal-looking result (exit_code: -9) and the
    agent loop reaches its next safe-point cancel check immediately.

    Also flushes every entry in `_ACTIVE_BG_PROCS` — background procs
    started by `run_background` share the same fate: a single Esc
    should leave nothing lingering.

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
    with _ACTIVE_BG_LOCK:
        bg_entries = list(_ACTIVE_BG_PROCS.values())
    for bg in bg_entries:
        try:
            os.killpg(bg.proc.pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
    return killed


def shutdown_background(grace_s: float = 2.0) -> int:
    """Clean-shutdown path for background procs.

    Sends SIGTERM to every active background-proc group, waits up to
    `grace_s` seconds total for them to exit, then SIGKILLs whatever
    remains. Called by the agent process's normal teardown so a
    `pyagent` exit doesn't leave the user's dev server lingering.

    Returns the number of process groups signalled (term + kill).
    """
    with _ACTIVE_BG_LOCK:
        bg_entries = list(_ACTIVE_BG_PROCS.values())
    if not bg_entries:
        return 0
    signalled = 0
    for bg in bg_entries:
        try:
            os.killpg(bg.proc.pid, signal.SIGTERM)
            signalled += 1
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + max(grace_s, 0.0)
    while time.monotonic() < deadline:
        if all(bg.proc.poll() is not None for bg in bg_entries):
            return signalled
        time.sleep(0.05)
    for bg in bg_entries:
        if bg.proc.poll() is None:
            try:
                os.killpg(bg.proc.pid, signal.SIGKILL)
                signalled += 1
            except ProcessLookupError:
                pass
    return signalled


def _denied(path: str) -> str:
    return f"<permission denied (outside workspace): {path}>"


# Memory tools (create_memory / read_memory / update_memory /
# delete_memory / write_user / recall_memory) live in the bundled
# memory plugin (pyagent/plugins/memory/). Disabling the plugin
# removes them entirely — clean replacement surface for alternative
# memory backends.


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


def grep(
    pattern: str,
    path: str,
    *,
    before: int = 0,
    after: int = 0,
    context: int = 0,
) -> list[str]:
    """Search for a regex pattern in a file or directory tree.

    First reach for any "where does X appear?" question — cheaper than
    reading whole files to skim. Searches recursively if `path` is a
    directory. Files that cannot be decoded as UTF-8 text are skipped.
    Output line numbers match `read_file`'s `start`/`end` so you can
    pipe a hit into a targeted read.

    With `before` / `after` / `context` set, surrounding lines come
    back in the same call so you don't need a follow-up `read_file`.
    Semantics mirror GNU `grep`'s `-B` / `-A` / `-C`: `context=N` is
    shorthand for `before=N, after=N`; an explicit non-zero `before`
    or `after` overrides the corresponding side of `context`.

    Output uses GNU-grep's separator convention: matched lines stay
    `path:lineno:line` (colon), while context lines use a dash —
    `path:lineno-line`. Adjacent matches whose context windows touch
    or overlap collapse into one contiguous excerpt with no duplicate
    lines. When context is non-zero, runs of output from the same
    file are separated by a `--` line.

    Args:
        pattern: Regex pattern to search for.
        path: File or directory to search.
        before: Number of lines of leading context per match.
        after: Number of lines of trailing context per match.
        context: Shorthand for `before=N, after=N`. Explicit `before`
            / `after` win when both are non-zero.

    Returns:
        List of matches. Without context: `path:lineno:line` per match.
        With context: matches keep the colon, surrounding lines use a
        dash, and same-file runs are separated by `--`.
    """
    try:
        before_i = int(before)
        after_i = int(after)
        context_i = int(context)
    except (TypeError, ValueError):
        return [
            f"<error: before/after/context must be integers, got "
            f"before={before!r}, after={after!r}, context={context!r}>"
        ]
    if before_i < 0 or after_i < 0 or context_i < 0:
        return [
            f"<error: before/after/context must be non-negative, got "
            f"before={before_i}, after={after_i}, context={context_i}>"
        ]
    # Explicit before/after override the matching side of context.
    eff_before = before_i if before_i > 0 else context_i
    eff_after = after_i if after_i > 0 else context_i

    if not permissions.require_access(path):
        return [_denied(path)]
    regex = re.compile(pattern)
    root = Path(path)
    if not root.exists():
        return [f"<path not found: {path}>"]
    candidates = [root] if root.is_file() else root.rglob("*")
    use_context = eff_before > 0 or eff_after > 0
    results: list[str] = []
    for f in sorted(candidates):
        if not f.is_file():
            continue
        try:
            text = f.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        lines = text.splitlines()
        match_idxs = [
            i for i, line in enumerate(lines) if regex.search(line)
        ]
        if not match_idxs:
            continue
        if not use_context:
            for i in match_idxs:
                results.append(f"{f}:{i + 1}:{lines[i]}")
            continue
        # Build collapsed groups: a new group starts when the next
        # match's leading context window doesn't touch the previous
        # group's trailing context window.
        match_set = set(match_idxs)
        groups: list[tuple[int, int]] = []  # inclusive (start, end) line idx
        cur_start = max(0, match_idxs[0] - eff_before)
        cur_end = min(len(lines) - 1, match_idxs[0] + eff_after)
        for m in match_idxs[1:]:
            m_start = max(0, m - eff_before)
            m_end = min(len(lines) - 1, m + eff_after)
            if m_start <= cur_end + 1:
                # Windows touch or overlap — extend.
                if m_end > cur_end:
                    cur_end = m_end
            else:
                groups.append((cur_start, cur_end))
                cur_start, cur_end = m_start, m_end
        groups.append((cur_start, cur_end))

        for gi, (g_start, g_end) in enumerate(groups):
            if gi > 0:
                results.append("--")
            for li in range(g_start, g_end + 1):
                sep = ":" if li in match_set else "-"
                results.append(f"{f}:{li + 1}{sep}{lines[li]}")
    return results


# Default exclusion globs for `glob`. Mirrors the
# shutil.ignore_patterns set bench_cli uses when seeding workspaces
# (see pyagent/bench_cli.py) so users see the same rules across pyagent.
# We deliberately don't parse `.gitignore` — that's scope creep; ad-hoc
# overrides should pass an explicit `root` and a tighter pattern.
_GLOB_DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "*.egg-info",
)


def _is_excluded(rel_parts: tuple[str, ...]) -> bool:
    """Return True if any path component matches a default exclusion."""
    for part in rel_parts:
        for pat in _GLOB_DEFAULT_EXCLUDES:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def glob(
    pattern: "str | list[str]",
    *,
    root: str = ".",
    limit: int = 200,
) -> list[str]:
    """Find files by name pattern under `root`.

    Patterns use Python `pathlib`-style semantics: `**/*.py` matches
    every `.py` file at any depth, `src/*.ts` matches one level deep
    under `src/`. Output paths are relative to `root` so they can be
    fed straight back into `read_file` / `grep`.

    Default exclusions: `.git`, `.venv`, `venv`, `node_modules`,
    `__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `dist`,
    `build`, `*.egg-info`. Pass a more specific `root` to look inside
    one of those (e.g. `root="node_modules/some-pkg"`).

    Args:
        pattern: Glob pattern to match, or a list of patterns. Lists
            save a round trip when you want both source and stub
            extensions (`["**/*.py", "**/*.pyi"]`); their results are
            merged and de-duplicated.
        root: Directory to search under. Defaults to `.`.
        limit: Maximum number of paths to return. Defaults to 200.
            When the cap fires, the result includes a trailing
            `<truncated: NNN total matches; tighten the pattern>`
            marker so you know to narrow.

    Returns:
        Sorted list of relative paths (relative to `root`). Predictable
        failures (root missing, root not a directory, root outside the
        workspace) come back as a single-element list with a leading
        `<...>` marker.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return [f"<error: limit must be an integer, got {limit!r}>"]
    if limit <= 0:
        return [f"<error: limit must be positive, got {limit}>"]

    if isinstance(pattern, str):
        patterns = [pattern]
    elif isinstance(pattern, list):
        patterns = [str(p) for p in pattern]
    else:
        return [f"<error: pattern must be str or list[str], got {type(pattern).__name__}>"]
    if not patterns:
        return ["<error: pattern list is empty>"]

    if not permissions.require_access(root):
        return [_denied(root)]

    root_path = Path(root)
    if not root_path.exists():
        return [f"<path not found: {root}>"]
    if not root_path.is_dir():
        return [f"<not a directory: {root}>"]

    matches: set[Path] = set()
    for pat in patterns:
        try:
            for hit in root_path.glob(pat):
                if not hit.is_file():
                    continue
                try:
                    rel = hit.relative_to(root_path)
                except ValueError:
                    # Pattern escaped the root via "..", skip.
                    continue
                if _is_excluded(rel.parts):
                    continue
                matches.add(rel)
        except (NotImplementedError, ValueError) as e:
            return [f"<error: invalid pattern {pat!r}: {e}>"]

    sorted_rel = sorted(str(p) for p in matches)
    total = len(sorted_rel)
    if total > limit:
        capped = sorted_rel[:limit]
        capped.append(
            f"<truncated: {total} total matches; tighten the pattern>"
        )
        return capped
    return sorted_rel


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
        r"\bpip\d?\s+install\b[^|;&]*\s--break-system-packages\b",
        "pip install bypassing PEP 668 (--break-system-packages)",
    ),
    (
        r"\bpip\d?\s+install\b[^|;&]*\s--user\b",
        "pip install --user (writes to ~/.local; use a project venv)",
    ),
    (
        r"\bsudo\b[^|;&]*\bpip\d?\s+install\b",
        "pip install under sudo (escalates to a shared interpreter)",
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


# ---------------------------------------------------------------------------
# Background shell processes — run_background / read_output / wait_for /
# kill_process. Lifecycle:
#   run_background   spawns + registers a handle, returns the handle id.
#   read_output      decodes the captured bytes since an offset.
#   wait_for         blocks (with timeout) until exit / output match /
#                    silence settles.
#   kill_process     SIGKILLs the group and removes the handle.
# kill_active() (above) flushes BOTH foreground and background sets so
# the cancel pathway leaves a clean slate.


def _bg_handle() -> str:
    return f"bg-{secrets.token_hex(4)}"


def _bg_reader(bg: _BackgroundProc, stream, which: str) -> None:
    """Pump bytes from `stream` into the combined output buffer.

    Holds the lock only across the buffer mutation so concurrent
    `read_output` / `wait_for` can keep observing while bytes flow.
    The 1MB rolling cap drops the oldest 256KB instead of growing
    unbounded — agents rarely care about start-of-stream once the
    process has been running for a while, but they do need recent
    output to be intact.

    On a stream transition (stdout→stderr or back), inserts a
    `[stderr]\\n` / `[stdout]\\n` marker so the agent can still tell
    sources apart in the combined log. Stdout-only processes (the
    common case) emit no markers.
    """
    try:
        while True:
            # `read1` returns whatever is currently buffered up to the
            # cap — `read(4096)` would block until 4096 bytes (or EOF),
            # which interacts badly with line-buffered tools that emit
            # short bursts and then sleep. Tail-follow needs the
            # bytes-out-now semantics.
            chunk = stream.read1(4096)
            if not chunk:
                break
            with bg.lock:
                # Insert a transition marker only when the source
                # actually changes; the initial state (last_source="")
                # treats stdout as the implicit default — no leading
                # `[stdout]` marker on processes that never use stderr.
                if which != bg.last_source and (
                    bg.last_source != "" or which != "stdout"
                ):
                    if bg.output_buf and not bg.output_buf.endswith(b"\n"):
                        bg.output_buf.append(0x0A)  # newline
                    bg.output_buf.extend(f"[{which}]\n".encode())
                bg.last_source = which
                bg.output_buf.extend(chunk)
                if len(bg.output_buf) > _BG_BUF_CAP:
                    overflow = len(bg.output_buf) - (
                        _BG_BUF_CAP - _BG_BUF_DROP
                    )
                    del bg.output_buf[:overflow]
                    bg.dropped += overflow
                bg.last_write = time.monotonic()
    except (ValueError, OSError):
        # Stream closed underneath us (proc died, fd reaped). The
        # main reader exits — wait_for / read_output handle the
        # post-mortem state via proc.poll().
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _coerce_handle(handle: str) -> "_BackgroundProc | str":
    """Look up a handle, returning either the entry or a `<...>` marker.

    Stale-handle responses follow the project's predictable-failure
    convention so the agent can pattern-match them just like a missing
    file or a permission denial.
    """
    if not isinstance(handle, str) or not handle:
        return f"<error: handle must be a non-empty string, got {handle!r}>"
    with _ACTIVE_BG_LOCK:
        bg = _ACTIVE_BG_PROCS.get(handle)
    if bg is None:
        return f"<error: handle {handle} is not active in this session>"
    return bg


def run_background(command: str, *, name: str | None = None) -> str:
    """Start a long-running shell command without blocking the turn.

    Use this for dev servers, file watchers, builds, test watchers, or
    anything you'd kick off and check on later. The handle returned
    here is what `read_output`, `wait_for`, and `kill_process` operate
    against. Foreground `execute` is still the right tool for short
    one-shot commands — keep using it for git, scripts, tests that
    finish in seconds, and HTTP one-offs.

    Same dangerous-pattern blocklist as `execute` (recursive rm at
    root, fork bombs, piping curl|sh, force push to main, etc.). The
    process runs in its own session so a later `kill_process` (or Esc
    via `kill_active`) takes the whole tree.

    Args:
        command: Shell command to run.
        name: Optional human-readable label (shows up in error markers
            and the wait_for status report). Defaults to None.

    Returns:
        A short confirmation line including the handle (`bg-XXXXXXXX`)
        and pid, or a `<refused: ...>` marker if the command matches a
        blocked pattern.
    """
    blocked = _safety_check(command)
    if blocked:
        return (
            f"<refused: matches dangerous pattern ({blocked}); "
            f"ask the human to run it manually if intended>"
        )
    proc = subprocess.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    handle = _bg_handle()
    label = name or handle
    bg = _BackgroundProc(
        handle=handle,
        name=label,
        command=command,
        proc=proc,
        start_time=time.monotonic(),
        last_write=time.monotonic(),
    )
    t_out = threading.Thread(
        target=_bg_reader,
        args=(bg, proc.stdout, "stdout"),
        name=f"bg-{handle}-out",
        daemon=True,
    )
    t_err = threading.Thread(
        target=_bg_reader,
        args=(bg, proc.stderr, "stderr"),
        name=f"bg-{handle}-err",
        daemon=True,
    )
    bg.threads = [t_out, t_err]
    t_out.start()
    t_err.start()
    with _ACTIVE_BG_LOCK:
        _ACTIVE_BG_PROCS[handle] = bg
    return (
        f"started {handle} (pid {proc.pid}, name={label}): {command}\n"
        f"use read_output({handle!r}) / wait_for({handle!r}) / "
        f"kill_process({handle!r})"
    )


def _decode_with_drop(buf: bytearray, since: int, dropped: int) -> tuple[str, int]:
    """Decode `buf[since:]` to text, prepending a drop-notice if any
    bytes have been evicted by the rolling cap.

    `since` is an absolute byte offset (counts every byte ever written
    to the stream, including ones the cap has since dropped). The
    buffer's index 0 corresponds to absolute byte `dropped`. We clamp
    the read forward when `since < dropped` and emit a
    `...truncated NN bytes...` notice so the agent knows it lost a
    prefix.

    Returns (text, total_seen) — `total_seen` is the absolute byte
    count for the next call to pass as `since` for tail-follow.
    """
    total_seen = dropped + len(buf)
    start = max(0, since - dropped)
    chunk = bytes(buf[start:])
    text = chunk.decode("utf-8", errors="replace")
    notice = ""
    if dropped > 0 and since < dropped:
        skipped = dropped - since
        notice = f"...truncated {skipped} bytes...\n"
    return notice + text, total_seen


def read_output(handle: str, *, since: int = 0, max_chars: int = 4000) -> str:
    """Read captured output from a background process.

    Returns whatever has been captured since byte offset `since`,
    capped at `max_chars`. Use the offset returned in the trailing
    `next_since:` line to tail-follow on subsequent calls without
    re-reading bytes you've already seen.

    stdout and stderr share one buffer; on a stream transition the
    reader inserts a `[stderr]\\n` / `[stdout]\\n` marker so the agent
    can tell sources apart. Exact temporal interleaving across
    streams isn't preserved — but the byte-position cursor is
    unambiguous.

    Args:
        handle: Handle returned by `run_background`.
        since: Byte offset to start reading from. Defaults to 0 (the
            start of the buffer, accounting for any rolling-cap drops).
        max_chars: Hard cap on the inline output. Defaults to 4000.
            Truncation appends a `...[N more chars; raise max_chars
            or read again with since=...]` marker.

    Returns:
        Multi-line block: a header naming the handle and process
        status, the captured output, and a `next_since:` line for
        tail-follow. Stale handles and bad arguments come back as
        `<...>` markers.
    """
    try:
        since = int(since)
    except (TypeError, ValueError):
        return f"<error: since must be an integer, got {since!r}>"
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        return f"<error: max_chars must be an integer, got {max_chars!r}>"
    if max_chars <= 0:
        return f"<error: max_chars must be positive, got {max_chars}>"

    bg = _coerce_handle(handle)
    if isinstance(bg, str):
        return bg

    with bg.lock:
        text, next_since = _decode_with_drop(
            bg.output_buf, since, bg.dropped
        )
    rc = bg.proc.poll()
    status = "running" if rc is None else f"exited (rc={rc})"

    truncated_chars = 0
    if len(text) > max_chars:
        truncated_chars = len(text) - max_chars
        text = text[:max_chars]

    header = f"{bg.handle} ({bg.name}) {status}"
    footer_lines = [f"next_since: {next_since}"]
    if truncated_chars:
        footer_lines.append(
            f"...[{truncated_chars} more chars; raise max_chars "
            f"or call again with a higher since]"
        )
    return f"{header}\n{text}\n" + "\n".join(footer_lines)


def _combined_text(bg: _BackgroundProc) -> str:
    """Snapshot the current combined output as text. With the
    single-buffer design, this is just the buffer decoded — stream
    transitions are already marked inline by the reader threads."""
    with bg.lock:
        return bytes(bg.output_buf).decode("utf-8", errors="replace")


def _tail(text: str, n: int = 400) -> str:
    if len(text) <= n:
        return text
    return "..." + text[-n:]


def wait_for(
    handle: str,
    *,
    timeout_s: float = 30,
    until: str = "exit",
) -> str:
    """Block (with timeout) until a background process meets a condition.

    `until` syntax:
      - `"exit"` — wait for the process to exit; returns the exit code.
      - `"output_contains:STRING"` — return when STRING appears in
        combined stdout+stderr.
      - `"output_matches:REGEX"` — same, but `re.search` against the
        combined output.
      - `"silence:Ns"` — return once N seconds have passed with no new
        bytes written. Useful for "the build settled."

    All conditions respect `timeout_s`; exceeding it returns
    `<timed out: ...>` with a tail of the captured output.

    Args:
        handle: Handle returned by `run_background`.
        timeout_s: Maximum seconds to block. Defaults to 30.
        until: Condition expression (see syntax above). Defaults to
            `"exit"`.

    Returns:
        Short status report (`matched`, `exited`, etc.) plus a tail of
        the captured output. Stale handles and bad arguments come back
        as `<...>` markers.
    """
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        return f"<error: timeout_s must be a number, got {timeout_s!r}>"
    if not isinstance(until, str) or not until:
        return f"<error: until must be a non-empty string, got {until!r}>"

    bg = _coerce_handle(handle)
    if isinstance(bg, str):
        return bg

    deadline = time.monotonic() + max(timeout_s, 0.0)
    poll_interval = 0.05

    if until == "exit":
        try:
            rc = bg.proc.wait(timeout=max(timeout_s, 0.0))
        except subprocess.TimeoutExpired:
            tail = _tail(_combined_text(bg))
            return (
                f"<timed out: {bg.handle} ({bg.name}) still running after "
                f"{timeout_s}s; tail:\n{tail}>"
            )
        tail = _tail(_combined_text(bg))
        return (
            f"exited {bg.handle} ({bg.name}) rc={rc}\ntail:\n{tail}"
        )

    if until.startswith("output_contains:"):
        needle = until[len("output_contains:"):]
        if not needle:
            return "<error: output_contains needs a non-empty substring>"
        while time.monotonic() < deadline:
            if needle in _combined_text(bg):
                tail = _tail(_combined_text(bg))
                return (
                    f"matched {bg.handle} ({bg.name}) on substring "
                    f"{needle!r}\ntail:\n{tail}"
                )
            if bg.proc.poll() is not None:
                # Process exited before we matched. Check one more time
                # in case the final bytes landed under the lock.
                if needle in _combined_text(bg):
                    tail = _tail(_combined_text(bg))
                    return (
                        f"matched {bg.handle} ({bg.name}) on substring "
                        f"{needle!r} (after exit)\ntail:\n{tail}"
                    )
                tail = _tail(_combined_text(bg))
                return (
                    f"<exited before match: {bg.handle} ({bg.name}) "
                    f"exited rc={bg.proc.returncode} without producing "
                    f"{needle!r}; tail:\n{tail}>"
                )
            time.sleep(poll_interval)
        tail = _tail(_combined_text(bg))
        return (
            f"<timed out: {bg.handle} ({bg.name}) did not produce "
            f"{needle!r} within {timeout_s}s; tail:\n{tail}>"
        )

    if until.startswith("output_matches:"):
        pattern = until[len("output_matches:"):]
        if not pattern:
            return "<error: output_matches needs a non-empty regex>"
        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"<error: invalid regex {pattern!r}: {e}>"
        while time.monotonic() < deadline:
            text = _combined_text(bg)
            m = regex.search(text)
            if m:
                tail = _tail(text)
                return (
                    f"matched {bg.handle} ({bg.name}) on regex "
                    f"{pattern!r} (match: {m.group(0)!r})\ntail:\n{tail}"
                )
            if bg.proc.poll() is not None:
                text = _combined_text(bg)
                m = regex.search(text)
                if m:
                    tail = _tail(text)
                    return (
                        f"matched {bg.handle} ({bg.name}) on regex "
                        f"{pattern!r} (after exit, match: "
                        f"{m.group(0)!r})\ntail:\n{tail}"
                    )
                tail = _tail(text)
                return (
                    f"<exited before match: {bg.handle} ({bg.name}) "
                    f"exited rc={bg.proc.returncode} without matching "
                    f"{pattern!r}; tail:\n{tail}>"
                )
            time.sleep(poll_interval)
        tail = _tail(_combined_text(bg))
        return (
            f"<timed out: {bg.handle} ({bg.name}) did not match "
            f"{pattern!r} within {timeout_s}s; tail:\n{tail}>"
        )

    if until.startswith("silence:"):
        spec = until[len("silence:"):]
        if spec.endswith("s"):
            spec = spec[:-1]
        try:
            quiet_s = float(spec)
        except ValueError:
            return f"<error: silence:Ns expected a number, got {spec!r}>"
        if quiet_s <= 0:
            return f"<error: silence:Ns must be positive, got {quiet_s}>"
        while time.monotonic() < deadline:
            now = time.monotonic()
            with bg.lock:
                last = bg.last_write
            if (now - last) >= quiet_s:
                tail = _tail(_combined_text(bg))
                return (
                    f"settled {bg.handle} ({bg.name}) — "
                    f"{quiet_s}s without new output\ntail:\n{tail}"
                )
            if bg.proc.poll() is not None:
                # Exited; treat the remaining quiet window as instantly
                # satisfied — no more output is coming.
                tail = _tail(_combined_text(bg))
                return (
                    f"settled {bg.handle} ({bg.name}) — process exited "
                    f"rc={bg.proc.returncode}\ntail:\n{tail}"
                )
            time.sleep(poll_interval)
        tail = _tail(_combined_text(bg))
        return (
            f"<timed out: {bg.handle} ({bg.name}) kept producing output "
            f"for {timeout_s}s without a {quiet_s}s quiet window; "
            f"tail:\n{tail}>"
        )

    return (
        f"<error: unknown until={until!r}; expected one of "
        f"'exit', 'output_contains:STRING', 'output_matches:REGEX', "
        f"'silence:Ns'>"
    )


def kill_process(handle: str) -> str:
    """SIGKILL a background process and forget the handle.

    Idempotent in spirit: a stale handle returns the standard
    `<error: handle ... is not active in this session>` marker. After
    a successful kill the handle is removed from the registry, so a
    follow-up `read_output` against it also returns the stale marker.

    Args:
        handle: Handle returned by `run_background`.

    Returns:
        Confirmation line with the final exit code (or `unknown` if
        the wait raced the kill). Stale handles come back as `<...>`.
    """
    bg = _coerce_handle(handle)
    if isinstance(bg, str):
        return bg
    try:
        os.killpg(bg.proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        # Already exited; fall through and clean up.
        pass
    try:
        rc = bg.proc.wait(timeout=2.0)
        rc_str = str(rc)
    except subprocess.TimeoutExpired:
        rc_str = "unknown (wait timed out)"
    with _ACTIVE_BG_LOCK:
        _ACTIVE_BG_PROCS.pop(bg.handle, None)
    return (
        f"killed {bg.handle} ({bg.name}) rc={rc_str}"
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


# Workspace pip install -------------------------------------------
#
# Routes installs through the workspace's `.venv/`, auto-creating it
# on first call. Registered ONLY on the root agent — subagents that
# want to install ask their parent via `ask_parent("install ...")`,
# which the root's LLM then translates into a `pip_install` call.
# This is the "parent-as-broker" pattern that #46 enables on top of
# #47, replacing the flock-serialization approach the issue
# originally proposed.

# Hard timeout for a single pip install. Long enough for fastembed-
# class downloads on a slow connection; short enough that a hung
# install can't wedge the agent forever.
_PIP_INSTALL_TIMEOUT_S = 600


def make_pip_install(workspace: Path):
    """Build the `pip_install` tool, closing over the workspace path.

    The factory is the seam that lets `_register_tools` decide
    whether to expose this on the root agent vs. wire it through
    `ask_parent` for subagents — see `agent_proc._register_tools`.
    """
    from pyagent import venv as venv_mod

    def pip_install(spec: str, venv: str = "") -> str:
        """Install a pip package into the workspace venv.

        On first call against a venv path, auto-creates that venv
        using the same Python interpreter the agent is running
        under. The default workspace `.venv/` is shared across
        this agent and any subagents (subagents `ask_parent` and
        the root does the install on their behalf, so concurrent
        installs serialize naturally).

        Args:
            spec: A pip-style package spec — `requests`,
                `requests==2.31.0`, `git+https://...`, or even a
                requirements file path. Whatever you'd pass to
                `pip install`.
            venv: Optional override for the target venv. Empty
                (the default) installs into the workspace's
                auto-discovered or auto-created `.venv/`. A
                relative path is resolved against the workspace
                (e.g. `".venv-test"` to keep test deps separate
                from the main runtime venv); an absolute path is
                used as-is. The venv is auto-created if missing.

        Returns:
            On success: a short summary including which venv was
            used and what pip reported. On failure: a `<...>`
            error marker with pip's stderr trimmed.
        """
        spec = (spec or "").strip()
        if not spec:
            return "<refused: empty package spec>"

        venv_arg = (venv or "").strip()
        try:
            if venv_arg:
                target = Path(venv_arg)
                if not target.is_absolute():
                    target = workspace / target
                venv_path, created = venv_mod.ensure_at(target)
            else:
                venv_path, created = venv_mod.ensure(workspace)
        except RuntimeError as e:
            return f"<venv setup failed: {e}>"

        notice = ""
        if created:
            notice = f"created venv at {venv_path}\n"

        pip_bin = venv_mod.pip_path(venv_path)
        if not pip_bin.exists():
            return (
                f"<venv at {venv_path} has no pip executable at "
                f"{pip_bin}; venv may be corrupt — delete and retry>"
            )

        # `--quiet` keeps the success path's output bounded; on
        # failure pip still emits the error. `--disable-pip-version-check`
        # silences a chatty notice that wastes context.
        cmd = [
            str(pip_bin),
            "install",
            "--quiet",
            "--disable-pip-version-check",
            spec,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PIP_INSTALL_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                f"<pip install {spec!r} timed out after "
                f"{_PIP_INSTALL_TIMEOUT_S}s>"
            )
        except FileNotFoundError as e:
            return f"<pip install failed: {e}>"

        if proc.returncode == 0:
            return (
                f"{notice}installed {spec} into {venv_path}"
                + (f"\n{proc.stdout.strip()}" if proc.stdout.strip() else "")
            )
        # Failure — surface stderr, capped so a verbose pip error
        # doesn't blow the conversation.
        err = (proc.stderr or proc.stdout or "").strip()
        if len(err) > 2000:
            err = err[:2000] + "\n…[truncated]"
        return (
            f"<pip install {spec!r} failed (rc={proc.returncode}):\n"
            f"{err}>"
        )

    return pip_install
