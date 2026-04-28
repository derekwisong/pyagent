"""Smoke for async subagent dispatch.

Covers:
  1. `_drain_pending_async()` — appends queued replies as user-role
     messages and clears the queue.
  2. `call_subagent_async` — sets entry.mode='async', returns a
     '<async call queued ...>' marker without blocking.
  3. IO thread routes a subagent's `turn_complete` to the parent's
     `pending_async_replies` inbox when entry.mode=='async', and
     leaves the per-sid sync reply queue empty so a future sync
     `call_subagent` doesn't pick up a stale value.
  4. `wait_for_subagents` returns immediately if replies are already
     queued, observes the cancel event, and respects timeout.
  5. Sync `call_subagent` refuses while another call is in-flight
     ('busy' marker).

In-process — no real LLM, uses pyagent/echo. Run with:

    .venv/bin/python -m tests.smoke_async_subagent
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
from pyagent import subagent
from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient
from pyagent.session import Session


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-async-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    for name in ("SOUL.md", "TOOLS.md", "PRIMER.md"):
        (tmp / name).write_text(f"# {name}\n")

    # 1. drain test (no subagent needed)
    standalone = Agent(client=EchoClient())
    standalone.pending_async_replies.put("[subagent A reports]: foo")
    standalone.pending_async_replies.put("[subagent B reports]: bar")
    drained = standalone._drain_pending_async()
    assert drained == 2, drained
    assert len(standalone.conversation) == 2, standalone.conversation
    assert standalone.conversation[0]["content"] == "[subagent A reports]: foo"
    assert standalone.conversation[1]["role"] == "user"
    assert standalone.pending_async_replies.empty()
    print("✓ _drain_pending_async appends and clears")

    # End-to-end: real subagent via pyagent/echo, async dispatch.
    parent_session = Session(root=tmp / "sessions")
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=upstream_state_end)
    agent = Agent(client=EchoClient(), session=parent_session, depth=0)
    state.agent = agent  # so the IO thread can find SubagentEntry by sid

    base_config = {
        "cwd": str(tmp),
        "model": "pyagent/echo",
        "soul_path": str(tmp / "SOUL.md"),
        "tools_path": str(tmp / "TOOLS.md"),
        "primer_path": str(tmp / "PRIMER.md"),
        "approved_paths": [],
    }
    spawn = subagent.make_spawn_subagent(
        state, agent, parent_session, base_config
    )
    call_sync = subagent.make_call_subagent(state, agent)
    call_async = subagent.make_call_subagent_async(state, agent)
    wait_for = subagent.make_wait_for_subagents(state, agent)
    terminate = subagent.make_terminate_subagent(state, agent)

    io_thread = threading.Thread(
        target=state.io_loop, name="test-io", daemon=True
    )
    io_thread.start()

    sid = ""
    try:
        sid = spawn("worker", "echo whatever")
        assert not sid.startswith("<"), sid
        entry = agent._subagents[sid]
        print(f"✓ spawned: {sid}")

        # 2. async fire returns immediately with queued marker.
        result = call_async(sid, "hello-async")
        assert "queued" in result and sid in result, result
        assert entry.mode == "async", entry.mode
        print(f"✓ async fired: {result[:60]}…")

        # 3. wait_for_subagents observes the reply showing up.
        before = time.monotonic()
        status = wait_for(timeout=10)
        elapsed = time.monotonic() - before
        assert "ready" in status, status
        assert elapsed < 5.0, f"wait took {elapsed:.1f}s"
        print(f"✓ wait_for_subagents: {status} ({elapsed:.2f}s)")

        # 4. The reply is on agent.pending_async_replies, formatted.
        assert agent.pending_async_replies.qsize() >= 1
        msg = agent.pending_async_replies.get_nowait()
        assert "subagent" in msg.lower(), msg
        assert "hello-async" in msg, msg
        assert sid in msg, msg
        print(f"✓ pending reply: {msg!r}")

        # 5. After delivery, mode is back to None and the per-sid
        #    sync reply queue stayed empty (no pollution).
        assert entry.mode is None, entry.mode
        with state._subagent_lock:
            sync_q = state._subagent_reply_queues.get(sid)
        assert sync_q is not None and sync_q.empty(), (
            f"sync reply queue contaminated: {list(sync_q.queue)}"
        )
        print("✓ entry.mode reset; sync queue uncontaminated")

        # 6. Now a sync call works again on the same subagent.
        reply = call_sync(sid, "hello-sync")
        assert reply == "hello-sync", reply
        print(f"✓ subsequent sync call works: {reply!r}")

        # 7. Busy guard: while async is in flight, sync should refuse.
        # Set mode manually to simulate (the IO thread's race with our
        # check is too tight to test reliably with a real subagent;
        # this just exercises the guard path).
        entry.mode = "async"
        result = call_sync(sid, "should-refuse")
        assert result.startswith("<subagent ") and "busy" in result, result
        print(f"✓ busy guard refuses sync during async: {result!r}")
        entry.mode = None  # clean up

        # 8. wait_for_subagents respects cancel.
        # Start a wait in another thread, set cancel_event, expect quick return.
        cancel_result: dict = {}

        def runner():
            cancel_result["v"] = wait_for(timeout=30)

        t = threading.Thread(target=runner, daemon=True)
        # Drain anything stale before the cancel test.
        while not agent.pending_async_replies.empty():
            agent.pending_async_replies.get_nowait()
        t.start()
        time.sleep(0.2)
        state.cancel_event.set()
        t.join(timeout=2.0)
        assert "cancelled" in cancel_result.get("v", ""), cancel_result
        state.cancel_event.clear()
        print(f"✓ wait_for_subagents respects cancel: {cancel_result['v']}")

        # 9. wait_for_subagents respects timeout.
        before = time.monotonic()
        # tiny timeout — should return promptly with timeout marker
        status = wait_for(timeout=1)
        elapsed = time.monotonic() - before
        assert "timed out" in status, status
        assert 0.9 < elapsed < 2.0, f"timeout elapsed {elapsed:.2f}s"
        print(f"✓ wait_for_subagents timeout: {status} ({elapsed:.2f}s)")

        terminate(sid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and entry.process.is_alive():
            time.sleep(0.05)
        assert not entry.process.is_alive()
    finally:
        for sid_, e in list(agent._subagents.items()):
            try:
                e.process.terminate()
                e.process.join(timeout=2)
            except Exception:
                pass
        state.shutdown_event.set()
        io_thread.join(timeout=2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
