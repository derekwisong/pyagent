"""Permission gating for filesystem access outside the workspace.

Tools that touch the filesystem call `require_access(path)` before
acting. Paths inside the workspace pass silently; paths outside prompt
the human (y / n / always). "always" answers are cached for the rest
of the session so the user is not re-prompted for adjacent files.

Non-interactive sessions (no TTY) deny by default — safer for
autonomous runs piped through automation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

_WORKSPACE: Path = Path.cwd().resolve()
_APPROVED: set[Path] = set()
_DENIED: set[Path] = set()
_PAUSE_IO: Callable[[], None] | None = None
_RESUME_IO: Callable[[], None] | None = None
_PROMPT_HANDLER: Callable[[Path], bool] | None = None


def set_io_hooks(
    pause: Callable[[], None] | None,
    resume: Callable[[], None] | None,
) -> None:
    """Register optional pause/resume callbacks invoked around the
    interactive prompt — e.g. to stop and restart the CLI spinner so
    its stdout writes don't garble the y/n/a question.
    """
    global _PAUSE_IO, _RESUME_IO
    _PAUSE_IO = pause
    _RESUME_IO = resume


def set_prompt_handler(handler: Callable[[Path], bool] | None) -> None:
    """Override the interactive y/n/a prompt with a custom handler.

    Used by an agent subprocess to marshal permission decisions back
    to the CLI process via IPC, instead of reading from stdin directly
    (which the CLI owns). The handler is responsible for calling
    `pre_approve(target)` itself if the user picked "always", since
    that detail is decided by the CLI but cached in the child.

    Pass `None` to fall back to the built-in stdin prompt.
    """
    global _PROMPT_HANDLER
    _PROMPT_HANDLER = handler


def set_workspace(path: str | Path) -> None:
    """Override the workspace root. Mostly for tests."""
    global _WORKSPACE
    _WORKSPACE = Path(path).resolve()


def workspace() -> Path:
    return _WORKSPACE


def approved_paths() -> frozenset[Path]:
    return frozenset(_APPROVED)


def pre_approve(path: str | Path) -> None:
    """Pre-approve a directory (and its contents) for tool access.

    Used at startup to whitelist the user's pyagent config dir so the
    agent can read/write its ledgers (USER.md, MEMORY.md) without
    prompting on every call.
    """
    _APPROVED.add(Path(path).resolve())


def deny(path: str | Path) -> None:
    """Hard-deny a specific file path. Equality-based, not prefix.

    Used to wall off individual marker/config files that should never
    be mutated by tools, even when their containing directory is
    pre-approved.
    """
    _DENIED.add(Path(path).resolve())


def require_access(path: str | Path) -> bool:
    """Return True if the agent may touch `path`.

    Resolves symlinks and `..` before checking, so a path string cannot
    sneak past the workspace boundary. Denied paths fail outright with
    no prompt. Inside the workspace: silent pass. Outside: prompt the
    human, cache "always" answers.
    """
    target = Path(path).resolve()
    if target in _DENIED:
        return False
    if target.is_relative_to(_WORKSPACE):
        return True
    if any(target.is_relative_to(p) for p in _APPROVED):
        return True
    return _prompt(target)


def _prompt(target: Path) -> bool:
    if _PROMPT_HANDLER is not None:
        return _PROMPT_HANDLER(target)
    if not sys.stdin.isatty():
        return False
    if _PAUSE_IO:
        _PAUSE_IO()
    try:
        sys.stderr.write(
            f"\nAgent is requesting access OUTSIDE the workspace:\n"
            f"  workspace: {_WORKSPACE}\n"
            f"  target:    {target}\n"
        )
        while True:
            sys.stderr.write(
                "Allow? [y]es / [n]o / [a]lways (this path and below): "
            )
            sys.stderr.flush()
            line = sys.stdin.readline()
            if not line:  # EOF — treat as denial rather than looping forever
                return False
            answer = line.strip().lower()
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            if answer in ("a", "always"):
                _APPROVED.add(target)
                return True
            sys.stderr.write(
                f"  unrecognized: {answer!r} — please answer y, n, or a\n"
            )
    finally:
        if _RESUME_IO:
            _RESUME_IO()
