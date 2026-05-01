"""Smoke for the typed-input queue (issue #42).

End-to-end testing of `_repl_async` requires a live PTY plus careful
timing to observe the busy window — out of scope for a fast smoke.
This locks the moving pieces that ARE unit-testable:

  1. `_queue_segment` rendering at 0 / 1 / N entries with truncation.
  2. `_handle_queue_command` semantics for /queue, /queue clear,
     /queue pop, and unknown subcommands.
  3. `_render_status_ansi` composition: base footer + queue segment
     + permission-pending notice all line up on one ANSI string.

Run with:

    .venv/bin/python -m tests.smoke_input_queue
"""

from __future__ import annotations

import collections
import io

from rich.console import Console

import pyagent.cli as cli_mod
from pyagent.cli import (
    _handle_queue_command,
    _queue_segment,
    _render_status_ansi,
)


def _strip_ansi(s: str) -> str:
    """Strip CSI escape sequences and leftover SGR resets so test
    assertions can match plain text without caring about styling."""
    import re
    # Remove CSI sequences (\x1b[...]).
    out = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)
    return out


def _capture_console(fn, *args, **kwargs) -> str:
    buf = io.StringIO()
    saved = cli_mod.console
    cli_mod.console = Console(file=buf, force_terminal=False, color_system=None)
    try:
        fn(*args, **kwargs)
    finally:
        cli_mod.console = saved
    return buf.getvalue()


def main() -> None:
    # 1. _queue_segment shapes
    q: collections.deque[str] = collections.deque()
    assert _queue_segment(q) == "", repr(_queue_segment(q))
    print("✓ empty queue → no segment")

    q.append("run the tests")
    seg = _queue_segment(q)
    assert seg == ' · queued: "run the tests"', repr(seg)
    print(f"✓ single entry: {seg!r}")

    q.append("then ship")
    q.append("and tell me")
    seg = _queue_segment(q)
    assert seg == ' · queued: 3 (next: "run the tests")', repr(seg)
    print(f"✓ multiple entries: {seg!r}")

    # Truncate long head
    q.clear()
    q.append("x" * 60)
    seg = _queue_segment(q)
    assert "…" in seg, seg
    assert len(seg) < 60, seg
    print(f"✓ long head truncated: {seg!r}")

    # 2. _handle_queue_command — print
    q.clear()
    q.append("first")
    q.append("second")
    out = _capture_console(_handle_queue_command, "/queue", q)
    assert "1." in out and "first" in out, out
    assert "2." in out and "second" in out, out
    assert len(q) == 2, list(q)
    print(f"✓ /queue prints contents (queue unchanged): {len(q)} entries")

    # 3. /queue clear
    out = _capture_console(_handle_queue_command, "/queue clear", q)
    assert "cleared 2" in out, out
    assert len(q) == 0, list(q)
    print("✓ /queue clear flushes the queue")

    out = _capture_console(_handle_queue_command, "/queue", q)
    assert "queue empty" in out, out
    print("✓ /queue on empty: 'queue empty'")

    # 4. /queue pop drops the *most recently added* (per the spec —
    #    that's the entry most likely to be a typo)
    q.append("a")
    q.append("b")
    q.append("c")
    out = _capture_console(_handle_queue_command, "/queue pop", q)
    assert "popped" in out and "'c'" in out, out
    assert list(q) == ["a", "b"], list(q)
    print(f"✓ /queue pop drops the tail: now {list(q)}")

    # 5. /queue pop on empty
    q.clear()
    out = _capture_console(_handle_queue_command, "/queue pop", q)
    assert "queue empty" in out, out
    print("✓ /queue pop on empty: 'queue empty'")

    # 6. Unknown subcommand — print error, queue unchanged
    q.append("only")
    out = _capture_console(_handle_queue_command, "/queue weird", q)
    assert "unknown" in out and "weird" in out, out
    assert len(q) == 1, list(q)
    print("✓ /queue weird → error, queue unchanged")

    # 7. _render_status_ansi composes base + queue + perm
    agents = {"root": {"status": "thinking"}}
    q.clear()
    out = _strip_ansi(_render_status_ansi(agents, "", q, None))
    assert out == "thinking…", repr(out)
    print(f"✓ ansi base only: {out!r}")

    q.append("foo")
    out = _strip_ansi(_render_status_ansi(agents, "", q, None))
    assert "thinking…" in out and 'queued: "foo"' in out, out
    print(f"✓ ansi base + queue: {out!r}")

    out = _strip_ansi(_render_status_ansi(agents, "", q, "/etc/passwd"))
    assert "awaiting permission" in out and "/etc/passwd" in out, out
    print(f"✓ ansi with permission pending: {out!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
