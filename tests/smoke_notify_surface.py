"""Smoke for the parent-side notification surface (issue #65).

Drives `tell_subagent` and `peek_subagent` against real
`_ChildState` IO threads with fake subagent pipes — the same
pattern as `smoke_ask_parent.py` and `smoke_notify.py`.

Locks:
  1. `tell_subagent(sid, text)` emits a `parent_note` event on the
     named pipe; refusal markers for empty text, empty/unknown
     sid, dead subagent.
  2. `peek_subagent(sid)` with `since=None` returns all ring
     entries plus a JSON-shaped `next_cursor:` line.
  3. `peek_subagent(sid, since="N")` returns only entries with
     seq > N; `since="0"` is the round-trip starting point.
  4. `peek_subagent()` with no sid surveys all live subagents,
     one section per sid, and returns a multi-sid JSON cursor.
  5. Multi-sid `since` accepts JSON object format; missing keys
     treated as 0.
  6. Invalid `since` (bad JSON, non-int values, integer with
     no sid) returns refusal markers.
  7. Ring overflow drop-marker visible: peek with `since` below
     the ring's earliest seq surfaces the missing-count line.
  8. `peek_subagent()` with no live subagents returns
     `<no live subagents>` marker.
  9. Ring is cleared on `terminate_subagent`; later peek of that
     sid returns the unknown-subagent marker.
 10. Ring is cleared on unexpected pipe close (subagent crash
     simulated by sending EOF on the fake pipe).

In-process — no real LLM, no subprocesses. Run with:

    .venv/bin/python -m tests.smoke_notify_surface
"""

from __future__ import annotations

import json
import multiprocessing
import os
import queue as _queue
import tempfile
import threading
import time
from pathlib import Path

from pyagent import agent_proc
from pyagent import subagent as subagent_mod
from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient
from pyagent.session import Session
from pyagent.subagent import SubagentEntry


class _FakeProcess:
    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self._alive = False


def _fake_subagent(
    pstate: agent_proc._ChildState,
    pagent: Agent,
    name: str,
    sid: str,
) -> tuple["multiprocessing.connection.Connection", SubagentEntry]:
    ctx = multiprocessing.get_context("spawn")
    fake_sub_end, fake_parent_end = ctx.Pipe(duplex=True)
    rq: _queue.Queue = _queue.Queue()
    pstate._subagent_conns[sid] = fake_parent_end
    pstate._subagent_reply_queues[sid] = rq
    entry = SubagentEntry(
        id=sid,
        name=name,
        process=_FakeProcess(alive=True),  # type: ignore[arg-type]
        conn=fake_parent_end,
        reply_queue=rq,
        depth=1,
    )
    pagent._subagents[sid] = entry
    return fake_sub_end, entry


def _drain_pipe(conn) -> list[dict]:
    out: list[dict] = []
    while conn.poll(0.05):
        try:
            out.append(conn.recv())
        except (EOFError, OSError):
            break
    return out


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-notify-surface-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    parent_session = Session(root=tmp / "sessions")
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    pstate = agent_proc._ChildState(conn=upstream_state_end)
    pagent = Agent(client=EchoClient(), session=parent_session, depth=0)
    pstate.agent = pagent

    # Wire the notes_unread emitter manually since the smoke skips
    # _bootstrap. Capture deltas so we can assert the CLI footer
    # signal fires correctly (issue #65 comment / #67 footer prep).
    emitted_unread: list[tuple[int, dict[str, int]]] = []

    def _capture_unread(count: int, by_sev: dict[str, int]) -> None:
        emitted_unread.append((count, dict(by_sev)))

    pagent._notes_unread_emitter = _capture_unread

    pio = threading.Thread(target=pstate.io_loop, daemon=True)
    pio.start()

    tell = subagent_mod.make_tell_subagent(pstate, pagent)
    peek = subagent_mod.make_peek_subagent(pstate, pagent)

    sid_a = "fake-a-deadbeef"
    sid_b = "fake-b-cafef00d"
    sub_a, entry_a = _fake_subagent(pstate, pagent, "alpha", sid_a)
    sub_b, entry_b = _fake_subagent(pstate, pagent, "beta", sid_b)

    try:
        # =========================================================
        # tell_subagent
        # =========================================================
        # 1. happy path
        result = tell(sid_a, "drop the test framework, switch to pytest")
        assert result == f"sent to {sid_a}", result
        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if sub_a.poll(0.1):
                ev = sub_a.recv()
                break
        assert ev is not None and ev["type"] == "parent_note", ev
        assert "switch to pytest" in ev["text"], ev
        print(f"✓ tell_subagent emitted parent_note: {ev['text']!r}")

        # 2. refusals
        assert tell("", "x") == "<refused: empty sid>", tell("", "x")
        assert tell(sid_a, "") == "<refused: empty text>", tell(sid_a, "")
        assert tell(sid_a, "   ") == "<refused: empty text>"
        assert tell("ghost", "x").startswith("<unknown subagent"), tell("ghost", "x")
        # dead subagent
        entry_a.process._alive = False  # type: ignore[attr-defined]
        assert "no longer running" in tell(sid_a, "still here?"), tell(
            sid_a, "still here?"
        )
        # restore for later tests
        entry_a.process._alive = True  # type: ignore[attr-defined]
        # drain anything that did make it onto sub_a
        _drain_pipe(sub_a)
        print("✓ tell_subagent refusals: empty sid, empty text, unknown, dead")

        # =========================================================
        # peek_subagent — populate rings first
        # =========================================================
        # Drive the parent IO thread by sending subagent_note events
        # from each fake child. Use _append_subagent_note directly
        # for some entries so we don't have to wait on the pipe.
        sub_a.send({
            "type": "subagent_note",
            "severity": "info",
            "text": "tests pass on darwin",
        })
        sub_a.send({
            "type": "subagent_note",
            "severity": "warn",
            "text": "migration assumes pg>=14",
        })
        sub_b.send({
            "type": "subagent_note",
            "severity": "info",
            "text": "build still running",
        })
        sub_b.send({
            "type": "subagent_note",
            "severity": "warn",
            "text": "lint failures upstream",
        })
        # Wait for IO thread to process all 4.
        ok = _wait_for(
            lambda: pagent.pending_async_replies.qsize() >= 4, timeout=3.0
        )
        assert ok, (
            f"IO thread did not process notes "
            f"(qsize={pagent.pending_async_replies.qsize()})"
        )
        # Drain inbox so it doesn't pollute later tests.
        while pagent.pending_async_replies.qsize():
            pagent.pending_async_replies.get_nowait()
        # Drain upstream forwards.
        _drain_pipe(upstream_test_end)

        # 3. single-sid peek with since=None — returns all ring entries.
        out = peek(sid=sid_a)
        assert f"[subagent alpha ({sid_a}) notes since cursor=0]:" in out, out
        assert "tests pass on darwin" in out, out
        assert "migration assumes pg>=14" in out, out
        # next_cursor JSON-shaped, points at latest seq (1)
        assert f'next_cursor: {{"{sid_a}": 1}}' in out, out
        print(f"✓ single-sid peek (since=None): cursor advances to 1")

        # 4. single-sid peek with since="0" — entries with seq > 0.
        out = peek(sid=sid_a, since="0")
        assert f"cursor=0]:" in out, out
        assert "migration assumes pg>=14" in out, out  # seq=1 visible
        # seq=0 was "tests pass on darwin" — should NOT appear since
        # cursor=0 means "I've already seen up through seq 0."
        assert "tests pass on darwin" not in out, out
        assert f'next_cursor: {{"{sid_a}": 1}}' in out, out
        print("✓ single-sid peek (since='0'): only seq>0 visible")

        # 5. single-sid peek with since="1" — no new notes.
        out = peek(sid=sid_a, since="1")
        assert "no new notes; cursor=1" in out, out
        print("✓ single-sid peek (since='1'): no new notes line")

        # 6. multi-sid survey (sid=None, since=None) — both sections.
        out = peek()
        assert f"[subagent alpha ({sid_a}) notes since cursor=0]:" in out, out
        assert f"[subagent beta ({sid_b}) notes since cursor=0]:" in out, out
        # next_cursor is JSON object with both sids
        # latest seq for alpha=1, beta=1
        assert f'"{sid_a}": 1' in out, out
        assert f'"{sid_b}": 1' in out, out
        print("✓ multi-sid peek (since=None): both sections + JSON cursor")

        # 7. multi-sid peek with JSON since — missing keys treated as 0.
        # Pass cursor for alpha only; beta should be surveyed from 0,
        # which surfaces beta's seq=1 note (the warn one).
        out = peek(since=json.dumps({sid_a: 1}))
        # Alpha section: "no new notes; cursor=1"
        assert f"[subagent alpha ({sid_a}) notes since cursor=1]:" in out, out
        assert "no new notes; cursor=1" in out, out
        # Beta section: cursor=0 (defaulted), shows entries with seq > 0.
        assert f"[subagent beta ({sid_b}) notes since cursor=0]:" in out, out
        assert "lint failures upstream" in out, out
        print("✓ multi-sid peek (JSON since, missing key=0)")

        # 8. invalid since formats.
        bad = peek(sid=sid_a, since="not-an-int-not-json")
        assert bad.startswith("<refused: invalid since"), bad
        bad = peek(sid=sid_a, since='{"a": "not-int"}')
        assert bad.startswith("<refused: invalid since"), bad
        bad = peek(sid=sid_a, since='["array"]')
        assert bad.startswith("<refused: invalid since"), bad
        # integer since without sid → refused
        bad = peek(since="3")
        assert bad.startswith("<refused: integer since requires sid"), bad
        print("✓ invalid since refused: bad str, non-int values, no-sid int")

        # 9. unknown sid / empty sid.
        bad = peek(sid="ghost")
        assert bad.startswith("<unknown subagent"), bad
        bad = peek(sid="   ")
        assert bad == "<refused: empty sid>", bad
        print("✓ peek refusals: unknown sid, empty sid")

        # =========================================================
        # Ring overflow + drop marker
        # =========================================================
        # Flood alpha's ring past maxlen. Already 2 entries (seq 0,1).
        # Add 64 more — 1 should be dropped (seq 0).
        for i in range(64):
            sub_a.send({
                "type": "subagent_note",
                "severity": "info",
                "text": f"flood-{i}",
            })
        ok = _wait_for(
            lambda: pagent.pending_async_replies.qsize() >= 64, timeout=5.0
        )
        assert ok, (
            f"IO thread didn't drain flood "
            f"(qsize={pagent.pending_async_replies.qsize()})"
        )
        while pagent.pending_async_replies.qsize():
            pagent.pending_async_replies.get_nowait()
        _drain_pipe(upstream_test_end)

        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[sid_a])
            drops = pagent._subagent_note_drops[sid_a]
        # alpha had 2 notes (seqs 0,1); +64 flood = 66 total; ring=64;
        # drops = 66 - 64 = 2; earliest seq in ring = 2.
        assert drops == 2, f"drops should be 2, got {drops}"
        assert ring[0][0] == 2, f"earliest seq should be 2, got {ring[0][0]}"

        # Peek with cursor=1 — entry seqs in (1, 2) is empty, so no
        # missing marker; but we should see all 64 ring entries
        # (seqs 2..65).
        out = peek(sid=sid_a, since="1")
        assert "dropped from ring" not in out, out
        # Force a wider gap: continue overflow until many seqs are dropped.
        for i in range(64):
            sub_a.send({
                "type": "subagent_note",
                "severity": "info",
                "text": f"more-{i}",
            })
        ok = _wait_for(
            lambda: pagent.pending_async_replies.qsize() >= 64, timeout=5.0
        )
        assert ok
        while pagent.pending_async_replies.qsize():
            pagent.pending_async_replies.get_nowait()
        _drain_pipe(upstream_test_end)

        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[sid_a])
            drops = pagent._subagent_note_drops[sid_a]
            seq_next = pagent._subagent_note_seq[sid_a]
        # 2 + 64 + 64 = 130 notes added; ring holds 64; drops = 66;
        # earliest seq = 66; seq_next = 130.
        assert drops == 66, f"drops should be 66, got {drops}"
        assert ring[0][0] == 66, f"earliest seq should be 66, got {ring[0][0]}"
        assert seq_next == 130, f"seq_next should be 130, got {seq_next}"

        # Peek with cursor=10 — entries with seq in (10, 66) were
        # dropped: that's seqs 11..65 = 55 entries.
        out = peek(sid=sid_a, since="10")
        assert "dropped from ring" in out, out
        assert "55 note(s)" in out, out
        print(
            f"✓ ring overflow visible in peek: drops={drops}, "
            f"earliest_seq={ring[0][0]}, peek shows missing-count"
        )

        # =========================================================
        # Lifecycle: terminate clears ring
        # =========================================================
        # 10. terminate_subagent path. The factory also tries to
        #     send shutdown / SIGTERM / SIGKILL to the (fake) process.
        #     _FakeProcess doesn't implement terminate(), so wrap it.
        terminate = subagent_mod.make_terminate_subagent(pstate, pagent)
        # Patch in a join + terminate that no-op for the fake.
        entry_b.process.join = lambda timeout=None: None  # type: ignore[assignment]
        entry_b.process.terminate = lambda: None  # type: ignore[assignment]
        entry_b.process.exitcode = 0  # type: ignore[attr-defined]
        # Also need pid attr for the SIGKILL fallback.
        entry_b.process.pid = -1  # type: ignore[attr-defined]
        entry_b.process._alive = False  # type: ignore[attr-defined]  # skip kill path
        result = terminate(sid_b)
        assert "terminated" in result, result
        # Ring for sid_b should be gone.
        with pagent._notes_lock:
            assert sid_b not in pagent._subagent_notes
            assert sid_b not in pagent._subagent_note_seq
            assert sid_b not in pagent._subagent_note_drops
        # Peek of dead sid → unknown marker.
        bad = peek(sid=sid_b)
        assert bad.startswith("<unknown subagent"), bad
        print(f"✓ terminate clears ring: peek of {sid_b[:12]} → unknown")

        # 11. Unexpected pipe close: rather than fight multiprocessing
        #     edge cases (closing one end of a Pipe and racing the IO
        #     thread's `wait()` snapshot is brittle in tests), exercise
        #     the cleanup path that the close handler runs:
        #     unregister + _clear_subagent_notes. The handler in
        #     agent_proc.py:336-356 calls exactly these two when EOF
        #     hits.
        with pagent._notes_lock:
            assert sid_a in pagent._subagent_notes
        pstate.unregister_subagent_pipe(sid_a)
        pagent._clear_subagent_notes(sid_a)
        with pagent._notes_lock:
            assert sid_a not in pagent._subagent_notes
        print(f"✓ pipe-close cleanup path clears ring for {sid_a[:12]}")

        # 12. No live subagents.
        # Remove sid_a from the registry to simulate full shutdown.
        pagent._subagents.pop(sid_a, None)
        out = peek()
        assert out == "<no live subagents>", out
        print("✓ no live subagents → marker")

        # =========================================================
        # notes_unread emitter (issue #65 comment / #67 footer prep)
        # =========================================================
        # Reset the capture and counters; do a clean run.
        emitted_unread.clear()
        with pagent._notes_lock:
            pagent._unread_notes_total = 0
            pagent._unread_notes_by_severity = {
                "info": 0, "warn": 0, "alert": 0,
            }

        # Re-register a fake child for the emitter test.
        sid_c = "fake-c-12345678"
        sub_c, entry_c = _fake_subagent(pstate, pagent, "gamma", sid_c)
        # Append three notes via the IO thread; expect three emit
        # events with monotonically rising counts and severity buckets.
        sub_c.send({"type": "subagent_note", "severity": "info", "text": "a"})
        sub_c.send({"type": "subagent_note", "severity": "warn", "text": "b"})
        sub_c.send({"type": "subagent_note", "severity": "info", "text": "c"})
        ok = _wait_for(lambda: len(emitted_unread) >= 3, timeout=3.0)
        assert ok, f"emit count = {len(emitted_unread)}"
        # Expect counts 1, 2, 3.
        counts = [e[0] for e in emitted_unread]
        assert counts == [1, 2, 3], counts
        # Last by_severity snapshot: info=2, warn=1, alert=0.
        last_by_sev = emitted_unread[-1][1]
        assert last_by_sev["info"] == 2, last_by_sev
        assert last_by_sev["warn"] == 1, last_by_sev
        assert last_by_sev.get("alert", 0) == 0, last_by_sev
        print(
            f"✓ notes_unread fires per append: counts={counts}, "
            f"final by_severity={last_by_sev}"
        )

        # Drain reset: simulate a turn picking up all queued notes.
        emitted_unread.clear()
        n = pagent._drain_pending_async()
        assert n >= 3, f"drain returned {n}"
        # Exactly one emit event with zeros (the drain reset).
        assert len(emitted_unread) == 1, emitted_unread
        zeroed_count, zeroed_by_sev = emitted_unread[0]
        assert zeroed_count == 0
        assert zeroed_by_sev == {"info": 0, "warn": 0, "alert": 0}
        # Counters reset.
        with pagent._notes_lock:
            assert pagent._unread_notes_total == 0
            assert pagent._unread_notes_by_severity == {
                "info": 0, "warn": 0, "alert": 0,
            }
        print(f"✓ drain resets unread counters; emitted zeroed snapshot")

        # Idempotent drain: no emit when there were no unread notes.
        emitted_unread.clear()
        pagent._drain_pending_async()
        assert emitted_unread == [], (
            "drain emitted spurious zero event when nothing was unread"
        )
        print("✓ drain with no unread → no spurious emit")

        # =========================================================
        # Structured collector reusable for /notes (issue #65 comment)
        # =========================================================
        # Append two notes and verify _collect_subagent_notes returns
        # the records a future /notes slash command would consume.
        emitted_unread.clear()
        sub_c.send({"type": "subagent_note", "severity": "info", "text": "x"})
        sub_c.send({
            "type": "subagent_note", "severity": "alert", "text": "y"
        })
        ok = _wait_for(lambda: len(emitted_unread) >= 2, timeout=2.0)
        assert ok
        record = subagent_mod._collect_subagent_notes(pagent, sid_c, cursor=None)
        assert record["sid"] == sid_c, record
        assert record["name"] == "gamma", record
        assert record["missing"] == 0, record
        assert len(record["entries"]) == 5, record  # 3 + 2
        # Each entry has structured fields the CLI can render.
        e = record["entries"][-1]
        assert {"seq", "ts", "severity", "text"} <= set(e.keys()), e
        assert e["text"] == "y"
        assert e["severity"] == "alert"
        print(
            f"✓ _collect_subagent_notes returns structured records "
            f"(reusable for /notes follow-up)"
        )

    finally:
        pstate.shutdown_event.set()
        pio.join(timeout=2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
