"""Shared path-formatting helpers for the toolkit's output bullets.

The wrapped CLIs disagree on what the `file` portion of a finding
should look like: ruff echoes the absolute filename it resolved
(regardless of what the caller passed), while mypy preserves the
caller's input (so a relative input stays relative). Same finding
shape, different path styles, depending on which tool ran. The
calling agent shouldn't have to know which it's looking at.

`shorten` normalizes both: paths inside cwd come back relative,
everything else stays absolute. Same answer regardless of which
upstream tool produced the finding.
"""

from __future__ import annotations

from pathlib import Path


def shorten(p: str) -> str:
    """Return `p` relative to cwd when it's under cwd, else `p`
    resolved-and-absolute. Errors (e.g. an empty/`?` placeholder)
    return the input unchanged so downstream formatting still gets
    *something*."""
    if not p or p == "?":
        return p
    try:
        resolved = Path(p).resolve()
        cwd = Path.cwd().resolve()
        return str(resolved.relative_to(cwd))
    except (ValueError, OSError):
        # ValueError: not under cwd. OSError: filesystem hiccup.
        try:
            return str(Path(p).resolve())
        except OSError:
            return p
