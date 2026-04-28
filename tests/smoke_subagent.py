"""End-to-end smoke for spawn_subagent / call_subagent / terminate_subagent.

In-process: builds a real Agent and a _ChildState whose upstream pipe
goes to the test itself (acting as the CLI). Wires the meta-tools via
the same factories used by agent_proc, runs `state.io_loop` on a
thread to multiplex the subagent pipe, and exercises the three tools
with `pyagent/echo` as the subagent's LLM (no network).

Run with:

    .venv/bin/python -m tests.smoke_subagent
"""

from __future__ import annotations

import multiprocessing
import os
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
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-subagent-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    # Persona files must exist for the spawn config dict (subagents
    # ignore them via is_subagent=True, but the keys must be present).
    for name in ("SOUL.md", "TOOLS.md", "PRIMER.md"):
        (tmp / name).write_text(f"# {name}\n")

    parent_session = Session(root=tmp / "sessions")

    # Upstream pipe stands in for the CLI in this test.
    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=upstream_state_end)

    agent = Agent(client=EchoClient(), session=parent_session, depth=0)

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
    call = subagent.make_call_subagent(state, agent)
    terminate = subagent.make_terminate_subagent(state, agent)

    io_thread = threading.Thread(
        target=state.io_loop, name="test-io", daemon=True
    )
    io_thread.start()

    sid = ""
    try:
        # 1. spawn → returns id, registry has entry, info event arrives upstream
        sid = spawn("worker", "Echo whatever the user says.")
        assert not sid.startswith("<"), f"spawn failed: {sid}"
        assert sid in agent._subagents, agent._subagents
        entry = agent._subagents[sid]
        assert entry.process.is_alive(), "subagent died right after spawn"
        assert entry.depth == 1, entry.depth
        print(f"✓ spawned: {sid} (depth={entry.depth})")

        # 2. call → echoes message back
        reply = call(sid, "hello world")
        assert reply == "hello world", f"unexpected reply: {reply!r}"
        print(f"✓ echoed: {reply!r}")

        # Second call to verify reply queue drains and reuses cleanly.
        reply2 = call(sid, "second message")
        assert reply2 == "second message", reply2
        print(f"✓ echoed (2nd): {reply2!r}")

        # 3. terminate → process exits, registry cleared
        result = terminate(sid)
        assert "terminated" in result.lower(), result
        # Give the proc a moment to fully reap.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and entry.process.is_alive():
            time.sleep(0.05)
        assert not entry.process.is_alive(), "subagent did not exit on terminate"
        assert sid not in agent._subagents, "registry not cleaned"
        print(f"✓ terminated: {result}")

        # Drain upstream events the IO thread forwarded for visibility.
        events: list[dict] = []
        while upstream_test_end.poll(0.1):
            try:
                events.append(upstream_test_end.recv())
            except (EOFError, OSError):
                break
        kinds = [e.get("type") for e in events]
        # The IO thread should have forwarded ready, info (spawned),
        # assistant_text from the echo turn(s), and info (terminated).
        assert "ready" in kinds, kinds
        assert any(
            e.get("type") == "info"
            and "spawned" in e.get("message", "")
            for e in events
        ), events
        assert "assistant_text" in kinds, kinds
        assert any(
            e.get("type") == "info"
            and "terminated" in e.get("message", "")
            for e in events
        ), events
        # Subagent events all carry agent_id == sid.
        sub_events = [e for e in events if e.get("agent_id")]
        assert all(e.get("agent_id") == sid for e in sub_events), sub_events
        print(f"✓ upstream events: {kinds}")
    finally:
        # Belt-and-suspenders: make sure no subagent process is left.
        for s_id, e in list(agent._subagents.items()):
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
