"""Routing smoke for recursive subagents.

Drives `_ChildState` directly with two simulated subagent pipes so we
can:
  1. Inject an event "from Y via X" upstream and assert it forwards to
     CLI with agent_id preserved AND that root learns the descendants
     route Y → X.
  2. Inject a CLI-bound event targeted at Y and assert it forwards
     down through X (preserving agent_id, since the strip happens at
     the deeper hop where Y is a direct child).
  3. Unregister X and assert the descendants route to Y is swept too.

True end-to-end recursion (an LLM-driven subagent that itself spawns
its own subagent) is verified by the manual real-API smoke; this is
the pure-IPC layer.

Run with:

    .venv/bin/python -m tests.smoke_recursive_subagent
"""

from __future__ import annotations

import multiprocessing
import time

from pyagent import agent_proc


def main() -> None:
    ctx = multiprocessing.get_context("spawn")
    cli_end, root_upstream_end = ctx.Pipe(duplex=True)
    root_to_x_end, x_test_end = ctx.Pipe(duplex=True)

    state = agent_proc._ChildState(conn=root_upstream_end)
    x_sid = "lead-deadbeef"
    state.register_subagent_pipe(x_sid, root_to_x_end)

    import threading

    io_thread = threading.Thread(
        target=state.io_loop, name="test-io", daemon=True
    )
    io_thread.start()

    y_sid = "helper-cafef00d"
    try:
        # 1. Bubble-up: simulate X forwarding an event from grandchild Y.
        #    From X's perspective, X's _forward_upstream stamped agent_id=Y
        #    before sending. The test writes that pre-stamped event onto
        #    root's pipe-to-X.
        x_test_end.send(
            {
                "type": "info",
                "level": "info",
                "message": "Y did a thing",
                "agent_id": y_sid,
            }
        )

        deadline = time.monotonic() + 3.0
        seen_y = False
        while time.monotonic() < deadline:
            if cli_end.poll(0.1):
                ev = cli_end.recv()
                if ev.get("agent_id") == y_sid and ev.get("type") == "info":
                    seen_y = True
                    break
        assert seen_y, "info from Y never reached the CLI"
        print(f"✓ Y info bubbled up to CLI with agent_id={y_sid}")

        with state._subagent_lock:
            via = state._descendants.get(y_sid)
        assert via == x_sid, (
            f"descendants table missing route: {state._descendants}"
        )
        print(f"✓ root learned descendants route: {y_sid} -> {x_sid}")

        # 2. Bubble-up of ready: same shape, but `kind in ('ready',
        #    'turn_complete', 'agent_error')` triggers the gated
        #    reply-queue path. With agent_id set, must skip the
        #    queue and STILL forward upstream + learn the route.
        # Drain anything stale first.
        while cli_end.poll(0.05):
            cli_end.recv()
        x_test_end.send(
            {"type": "ready", "agent_id": y_sid}
        )
        deadline = time.monotonic() + 3.0
        seen_ready = False
        while time.monotonic() < deadline:
            if cli_end.poll(0.1):
                ev = cli_end.recv()
                if ev.get("type") == "ready" and ev.get("agent_id") == y_sid:
                    seen_ready = True
                    break
        assert seen_ready, "ready from Y did not bubble up"
        # X's reply queue should NOT have received Y's ready (would
        # confuse a waiting spawn_subagent).
        with state._subagent_lock:
            x_rq = state._subagent_reply_queues.get(x_sid)
        assert x_rq is not None and x_rq.empty(), (
            f"X's reply queue got bubble-up event: {list(x_rq.queue)}"
        )
        print("✓ Y ready bubbled up; X's reply queue stayed empty")

        # 3. Downward routing: CLI sends a permission_response targeted
        #    at Y. Root forwards to X (via the descendants route)
        #    preserving agent_id. X (here, the test) reads it from
        #    x_test_end with agent_id still set so the deeper hop can
        #    do the strip.
        cli_end.send(
            {
                "type": "permission_response",
                "decision": True,
                "always": False,
                "agent_id": y_sid,
            }
        )
        deadline = time.monotonic() + 3.0
        forwarded_to_x = None
        while time.monotonic() < deadline:
            if x_test_end.poll(0.1):
                forwarded_to_x = x_test_end.recv()
                break
        assert forwarded_to_x is not None, (
            "downward event never reached X via descendants route"
        )
        assert forwarded_to_x.get("type") == "permission_response"
        assert forwarded_to_x.get("agent_id") == y_sid, (
            f"agent_id stripped too early: {forwarded_to_x}"
        )
        assert forwarded_to_x.get("decision") is True
        print(f"✓ permission_response routed Y via X: {forwarded_to_x}")

        # 4. Unregister X — sweep stale descendants entries.
        state.unregister_subagent_pipe(x_sid)
        with state._subagent_lock:
            assert y_sid not in state._descendants, (
                f"stale route after X unregister: {state._descendants}"
            )
        print("✓ unregister X swept descendants route to Y")
    finally:
        state.shutdown_event.set()
        io_thread.join(timeout=2)
        for c in (cli_end, root_upstream_end, root_to_x_end, x_test_end):
            try:
                c.close()
            except Exception:
                pass

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
