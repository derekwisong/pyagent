"""Smoke for the CLI submit-handler state machine (issues #68 + #69).

Replaces the old `smoke_input_queue.py` — issue #68 deletes the
local input queue and replaces it with mid-turn `user_note`
injection; issue #69 replaces the single-slot `perm_pending` with a
FIFO of pending permission requests routed by `request_id`.

Locks the unit-testable pieces:

  1. `_perms_segment` rendering at 0 / 1 / N entries with head
     preview truncation.
  2. `_handle_perms_command` semantics for /perms (list),
     /perms <n> (rotate index n to head), /perms <bad>
     (unknown), /perms on empty.
  3. `_render_status_ansi` composition: base footer + perms
     segment.
  4. Spinner segment (unchanged from #42 — guarded against
     regression).

End-to-end testing of `_repl_async` requires a live PTY plus
careful timing — out of scope here. The integration of
`user_note` with the agent process is covered separately by the
agent-side unit test of `_handle_event` (see test 5 below, which
drives a fake `_ChildState` and verifies user_note routing).

Run with:

    .venv/bin/python -m tests.smoke_submit_handler
"""

from __future__ import annotations

import collections
import io
import multiprocessing
import threading
import time

from rich.console import Console

import pyagent.cli as cli_mod
from pyagent import agent_proc
from pyagent.agent import Agent
from pyagent.cli import (
    _SPINNER_FRAMES,
    _handle_perms_command,
    _perms_segment,
    _render_status_ansi,
    _spinner_segment,
)
from pyagent.llms.pyagent import EchoClient


def _strip_ansi(s: str) -> str:
    import re
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", s)


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
    # =========================================================
    # 1. _perms_segment shapes
    # =========================================================
    p: collections.deque[dict] = collections.deque()
    assert _perms_segment(p) == "", repr(_perms_segment(p))
    print("✓ empty perms → no segment")

    p.append({"target": "/etc/passwd", "agent_id": None, "request_id": "r1"})
    seg = _perms_segment(p)
    assert seg == " · perms: /etc/passwd", repr(seg)
    print(f"✓ single entry: {seg!r}")

    p.append({"target": "/usr/bin/x", "agent_id": "sub-1", "request_id": "r2"})
    p.append({"target": "/var/log/y", "agent_id": "sub-2", "request_id": "r3"})
    seg = _perms_segment(p)
    assert seg == " · perms: 3 (head: /etc/passwd)", repr(seg)
    print(f"✓ multiple entries: {seg!r}")

    # Long target truncation.
    p.clear()
    p.append({"target": "/" + "x" * 60, "agent_id": None, "request_id": "r"})
    seg = _perms_segment(p)
    assert "…" in seg, seg
    assert len(seg) < 60, seg
    print(f"✓ long head truncated: {seg!r}")

    # =========================================================
    # 2. _handle_perms_command — print / rotate / errors
    # =========================================================
    p.clear()
    p.append({"target": "/a", "agent_id": "sub-A", "request_id": "ra"})
    p.append({"target": "/b", "agent_id": "sub-B", "request_id": "rb"})
    p.append({"target": "/c", "agent_id": None, "request_id": "rc"})
    out = _capture_console(_handle_perms_command, "/perms", p)
    assert "1." in out and "/a" in out and "(active)" in out, out
    assert "2." in out and "/b" in out, out
    assert "3." in out and "/c" in out, out
    assert len(p) == 3, list(p)
    print(f"✓ /perms lists pending requests; head marked active")

    # Rotate: /perms 2 → entry at index 2 becomes head.
    out = _capture_console(_handle_perms_command, "/perms 2", p)
    assert "active: /b" in out, out
    targets = [e["target"] for e in p]
    assert targets == ["/b", "/a", "/c"], targets
    print(f"✓ /perms 2 rotates index 2 to head: {targets}")

    # Already active.
    out = _capture_console(_handle_perms_command, "/perms 1", p)
    assert "already active" in out, out
    print("✓ /perms 1 → already active")

    # Out of range.
    out = _capture_console(_handle_perms_command, "/perms 99", p)
    assert "out of range" in out, out
    print("✓ /perms 99 → out of range")

    # Unknown subcommand (non-numeric, non-empty).
    out = _capture_console(_handle_perms_command, "/perms weird", p)
    assert "unknown perms command" in out, out
    print("✓ /perms weird → error")

    # Empty queue listing.
    p.clear()
    out = _capture_console(_handle_perms_command, "/perms", p)
    assert "no pending permission requests" in out, out
    print("✓ /perms on empty → friendly message")

    # /perms <n> on empty.
    out = _capture_console(_handle_perms_command, "/perms 1", p)
    assert "no pending permission requests" in out, out
    print("✓ /perms 1 on empty → friendly message")

    # =========================================================
    # 3. _render_status_ansi composes base + perms
    # =========================================================
    agents = {"root": {"status": "thinking"}}
    p.clear()
    out = _strip_ansi(_render_status_ansi(agents, "", p))
    assert out == "thinking…", repr(out)
    print(f"✓ ansi base only: {out!r}")

    p.append({"target": "/etc/passwd", "agent_id": None, "request_id": "r1"})
    out = _strip_ansi(_render_status_ansi(agents, "", p))
    assert "thinking…" in out and "perms: /etc/passwd" in out, out
    print(f"✓ ansi base + single perm: {out!r}")

    p.append({"target": "/var/secrets", "agent_id": "sub", "request_id": "r2"})
    out = _strip_ansi(_render_status_ansi(agents, "", p))
    assert "perms: 2 (head: /etc/passwd)" in out, out
    print(f"✓ ansi base + multi-perm: {out!r}")

    # =========================================================
    # 4. Spinner regression check (unchanged from issue #42)
    # =========================================================
    assert _spinner_segment(False) == "", repr(_spinner_segment(False))
    print("✓ spinner hidden at idle")

    busy_seg = _spinner_segment(True)
    visible = _strip_ansi(busy_seg).strip()
    assert visible in _SPINNER_FRAMES, (
        f"unexpected spinner glyph {visible!r}"
    )
    print(f"✓ spinner busy frame: {visible!r}")

    # =========================================================
    # 5. user_note routing in agent_proc IO thread (issue #68)
    # =========================================================
    # Mid-turn injection vs. idle-window promotion: the IO thread
    # consults state.turn_active to decide whether to land the
    # note on pending_async_replies or promote to a fresh
    # user_prompt on the work_queue.
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=upstream_state_end)
    state.agent = Agent(client=EchoClient())
    io = threading.Thread(target=state.io_loop, daemon=True)
    io.start()

    try:
        # Case A: turn_active set → note lands on pending_async_replies.
        state.turn_active.set()
        upstream_test_end.send({"type": "user_note", "text": "use pytest"})
        deadline = time.monotonic() + 2.0
        msg = None
        while time.monotonic() < deadline:
            if state.agent.pending_async_replies.qsize() >= 1:
                msg = state.agent.pending_async_replies.get_nowait()
                break
            time.sleep(0.02)
        assert msg is not None, "user_note not routed to inbox"
        assert msg == "[user adds]: use pytest", msg
        # Nothing on work_queue (no promotion when active).
        try:
            stray = state.work_queue.get_nowait()
        except Exception:
            stray = None
        assert stray is None, f"user_note shouldn't promote when active: {stray}"
        print(f"✓ turn_active → user_note → pending_async_replies: {msg!r}")

        # Case B: turn_active cleared → note promotes to user_prompt.
        state.turn_active.clear()
        upstream_test_end.send({"type": "user_note", "text": "start a new task"})
        deadline = time.monotonic() + 2.0
        promoted = None
        while time.monotonic() < deadline:
            try:
                promoted = state.work_queue.get_nowait()
                break
            except Exception:
                time.sleep(0.02)
        assert promoted is not None, "idle-window note didn't promote"
        assert promoted.get("type") == "user_prompt", promoted
        assert promoted.get("prompt") == "start a new task", promoted
        # Inbox unchanged from earlier read.
        assert state.agent.pending_async_replies.qsize() == 0, (
            "idle-window note polluted inbox"
        )
        print(f"✓ idle window → user_note → promoted to user_prompt: "
              f"{promoted['prompt']!r}")

        # Case C: empty/whitespace text dropped silently.
        upstream_test_end.send({"type": "user_note", "text": "   "})
        time.sleep(0.2)
        try:
            stray = state.work_queue.get_nowait()
        except Exception:
            stray = None
        assert stray is None, f"empty user_note shouldn't propagate: {stray}"
        assert state.agent.pending_async_replies.qsize() == 0
        print("✓ empty user_note dropped silently")

    finally:
        state.shutdown_event.set()
        io.join(timeout=2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
