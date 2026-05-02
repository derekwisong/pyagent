"""Smoke for the subagent ↔ parent notification protocol (issue #64).

Drives both directions of the new `subagent_note` / `parent_note`
pipe events against real `_ChildState` IO threads without spawning
subagent subprocesses — Pipe pairs simulate the cross-process
channel so we can both inject events at the "subagent" and observe
what the parent does with them.

Locks:
  1. Subagent side: `notify_parent("text")` emits a `subagent_note`
     event upstream and returns immediately (non-blocking).
  2. Subagent side: empty / whitespace text and unknown severity
     return `<refused: ...>` markers without emitting upstream.
  3. Parent side: an inbound `subagent_note` from a (fake) direct
     child appends to the per-sid ring with a monotonic seq, sets
     the t0 anchor, and queues a formatted user-role message onto
     `pending_async_replies`.
  4. Parent side: a `subagent_note` arriving with `agent_id` set
     (bubbled from a grandchild) is dropped — no inbox injection,
     no ring append.
  5. Parent side: ring overflow drops the oldest entry and
     increments the per-sid drop counter; the seq counter never
     reuses values.
  6. Subagent side: an inbound `parent_note` queues a `[parent
     says]: ...` formatted message onto its own
     `pending_async_replies`.

In-process — no real LLM, no subprocesses. Run with:

    .venv/bin/python -m tests.smoke_notify
"""

from __future__ import annotations

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


def _make_subagent_state(
    tmp: Path,
) -> tuple[
    agent_proc._ChildState,
    "multiprocessing.connection.Connection",
    threading.Thread,
]:
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=upstream_state_end)
    state.self_agent_id = "fake-sub"
    state.agent = Agent(client=EchoClient())
    io = threading.Thread(target=state.io_loop, daemon=True)
    io.start()
    return state, upstream_test_end, io


def _drain_pipe(conn) -> list[dict]:
    out: list[dict] = []
    while conn.poll(0.05):
        try:
            out.append(conn.recv())
        except (EOFError, OSError):
            break
    return out


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-notify-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    # =========================================================
    # Subagent-side flows
    # =========================================================
    state, upstream, io = _make_subagent_state(tmp)
    notify = subagent_mod.make_notify_parent(state, state.agent)

    try:
        # 1. happy path: notify emits subagent_note and returns.
        result = notify("framing is off; switching approach", "alert")
        assert result.startswith("note sent"), result
        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if upstream.poll(0.1):
                ev = upstream.recv()
                break
        assert ev is not None, "no subagent_note emitted"
        assert ev["type"] == "subagent_note", ev
        assert ev["severity"] == "alert", ev
        assert "framing is off" in ev["text"], ev
        # No request_id / agent_id on the wire — notes are not
        # requests and target the immediate parent only.
        assert "request_id" not in ev, ev
        print(f"✓ notify emitted subagent_note: severity={ev['severity']!r}")

        # 2. default severity is "info".
        notify("everything fine")
        ev = None
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if upstream.poll(0.1):
                ev = upstream.recv()
                break
        assert ev is not None and ev["severity"] == "info", ev
        print("✓ default severity = 'info'")

        # 3. empty text refused without emitting.
        refused = notify("   ")
        assert refused == "<refused: empty text>", refused
        assert not upstream.poll(0.2), "refused notify still emitted"
        print(f"✓ empty text refused: {refused!r}")

        # 4. unknown severity refused without emitting.
        refused = notify("hi", severity="blocker")
        assert refused.startswith("<refused: severity 'blocker'"), refused
        assert not upstream.poll(0.2), "bad-severity notify still emitted"
        print(f"✓ unknown severity refused: {refused!r}")

        # 5. parent_note arriving on subagent side queues formatted msg.
        upstream.send({"type": "parent_note", "text": "stop and switch to pytest"})
        deadline = time.monotonic() + 2.0
        msg = None
        while time.monotonic() < deadline:
            if state.agent.pending_async_replies.qsize() >= 1:
                msg = state.agent.pending_async_replies.get_nowait()
                break
            time.sleep(0.05)
        assert msg is not None, "parent_note not queued onto inbox"
        assert msg.startswith("[parent says]:"), msg
        assert "stop and switch to pytest" in msg, msg
        print(f"✓ parent_note queued on subagent inbox: {msg!r}")

    finally:
        state.shutdown_event.set()
        io.join(timeout=2)

    # =========================================================
    # Parent-side flows
    # =========================================================
    parent_session = Session(root=tmp / "sessions")
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    pstate = agent_proc._ChildState(conn=upstream_state_end)
    pagent = Agent(client=EchoClient(), session=parent_session, depth=0)
    pstate.agent = pagent

    fake_sub_end, fake_parent_end = ctx.Pipe(duplex=True)
    fake_sid = "fake-sub-cafefade"
    pstate._subagent_conns[fake_sid] = fake_parent_end
    pstate._subagent_reply_queues[fake_sid] = _queue.Queue()
    pagent._subagents[fake_sid] = SubagentEntry(
        id=fake_sid,
        name="fake",
        process=_FakeProcess(alive=True),  # type: ignore[arg-type]
        conn=fake_parent_end,
        reply_queue=pstate._subagent_reply_queues[fake_sid],
        depth=1,
    )

    pio = threading.Thread(target=pstate.io_loop, daemon=True)
    pio.start()

    try:
        # 6. inbound subagent_note appends to ring and queues inbox msg.
        fake_sub_end.send({
            "type": "subagent_note",
            "severity": "warn",
            "text": "tests pass on darwin",
        })
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if pagent.pending_async_replies.qsize() >= 1:
                break
            time.sleep(0.05)
        assert pagent.pending_async_replies.qsize() == 1, (
            f"pending_async_replies did not pick up the note "
            f"(qsize={pagent.pending_async_replies.qsize()})"
        )
        msg = pagent.pending_async_replies.get_nowait()
        assert "fake" in msg and fake_sid in msg, msg
        assert "(warn)" in msg, msg
        assert "tests pass on darwin" in msg, msg
        # Ring populated with seq=0.
        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[fake_sid])
            seq_next = pagent._subagent_note_seq[fake_sid]
            drops = pagent._subagent_note_drops[fake_sid]
        assert len(ring) == 1, ring
        assert ring[0][0] == 0, ring  # seq
        assert ring[0][2] == "warn", ring  # severity
        assert ring[0][3] == "tests pass on darwin", ring
        assert seq_next == 1, seq_next
        assert drops == 0, drops
        print(f"✓ parent ring populated: seq=0, drops=0; inbox: {msg!r}")

        # The IO thread also forwards upstream so the CLI can render.
        # Drain that off the upstream test end.
        forwarded = _drain_pipe(upstream_test_end)
        assert any(
            e.get("type") == "subagent_note"
            and e.get("agent_id") == fake_sid
            for e in forwarded
        ), forwarded
        print(f"✓ subagent_note forwarded upstream with agent_id={fake_sid}")

        # 7. Bubbled (grandchild) subagent_note is dropped: no inbox
        #    injection, no ring append. We surface it upstream so the
        #    human sees something went sideways.
        fake_sub_end.send({
            "type": "subagent_note",
            "severity": "info",
            "text": "from a grandchild",
            "agent_id": "grandchild-id",  # makes inner_id non-None
        })
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
            forwarded = _drain_pipe(upstream_test_end)
            if forwarded:
                break
        # Inbox unchanged (still 0 — we drained the warn one above).
        assert pagent.pending_async_replies.qsize() == 0, (
            "bubbled note polluted parent inbox"
        )
        # Ring unchanged.
        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[fake_sid])
        assert len(ring) == 1, "bubbled note appended to ring"
        # Forwarded upstream so the human sees it.
        assert any(
            e.get("type") == "subagent_note"
            and e.get("text") == "from a grandchild"
            for e in forwarded
        ), forwarded
        print("✓ bubbled subagent_note dropped (no inbox / ring change)")

        # 8. Ring overflow: feed 64 more notes (we already have 1),
        #    so total = 65 → 1 drop expected.
        for i in range(64):
            fake_sub_end.send({
                "type": "subagent_note",
                "severity": "info",
                "text": f"flood-{i}",
            })
        # Wait until 64 more inbox messages have queued.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pagent.pending_async_replies.qsize() >= 64:
                break
            time.sleep(0.05)
        assert pagent.pending_async_replies.qsize() == 64, (
            f"flood not drained into inbox "
            f"(qsize={pagent.pending_async_replies.qsize()})"
        )
        # Drain the inbox so it doesn't pollute later tests.
        while pagent.pending_async_replies.qsize():
            pagent.pending_async_replies.get_nowait()
        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[fake_sid])
            seq_next = pagent._subagent_note_seq[fake_sid]
            drops = pagent._subagent_note_drops[fake_sid]
        assert len(ring) == 64, f"ring not full: len={len(ring)}"
        assert seq_next == 65, f"seq_next != 65: {seq_next}"
        assert drops == 1, f"drops != 1: {drops}"
        # The leftmost entry is now seq=1 (seq=0 was the first 'warn'
        # note from test 6, which got evicted).
        assert ring[0][0] == 1, f"ring[0].seq != 1: {ring[0][0]}"
        assert ring[0][3] == "flood-0", ring[0]
        # The rightmost entry is seq=64.
        assert ring[-1][0] == 64, f"ring[-1].seq != 64: {ring[-1][0]}"
        assert ring[-1][3] == "flood-63", ring[-1]
        print(
            f"✓ ring overflow: len={len(ring)}, seq_next={seq_next}, "
            f"drops={drops}, leftmost seq={ring[0][0]}"
        )

        # 9. More overflow: 10 more notes → drops = 11 cumulative.
        for i in range(10):
            fake_sub_end.send({
                "type": "subagent_note",
                "severity": "info",
                "text": f"more-{i}",
            })
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if pagent.pending_async_replies.qsize() >= 10:
                break
            time.sleep(0.05)
        # Drain inbox.
        while pagent.pending_async_replies.qsize():
            pagent.pending_async_replies.get_nowait()
        with pagent._notes_lock:
            ring = list(pagent._subagent_notes[fake_sid])
            drops = pagent._subagent_note_drops[fake_sid]
        assert drops == 11, f"drops should be 11: got {drops}"
        # Leftmost seq is now 11 (seqs 0..10 dropped).
        assert ring[0][0] == 11, ring[0]
        print(f"✓ continued overflow: drops={drops}, leftmost seq={ring[0][0]}")

        # 10. _clear_subagent_notes drops the per-sid state cleanly.
        pagent._clear_subagent_notes(fake_sid)
        with pagent._notes_lock:
            assert fake_sid not in pagent._subagent_notes
            assert fake_sid not in pagent._subagent_note_seq
            assert fake_sid not in pagent._subagent_note_drops
            assert fake_sid not in pagent._subagent_note_t0
        print("✓ _clear_subagent_notes wipes per-sid state")

    finally:
        pstate.shutdown_event.set()
        pio.join(timeout=2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
