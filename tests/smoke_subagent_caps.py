"""Smoke for spawn_subagent depth and fan-out caps.

Both caps come from <config-dir>/config.toml; we install a temporary
config that sets them low (max_fanout=1, max_depth=1) and verify
spawn_subagent refuses appropriately. Refusals are returned as
leading-`<` error markers, not raised, so the LLM sees them and adapts.

Run with:

    .venv/bin/python -m tests.smoke_subagent_caps
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
from pathlib import Path

from pyagent import agent_proc
from pyagent import config as config_mod
from pyagent import paths
from pyagent import subagent
from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient
from pyagent.session import Session


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-subagent-caps-"))
    os.chdir(tmp)

    for name in ("SOUL.md", "TOOLS.md", "PRIMER.md"):
        (tmp / name).write_text(f"# {name}\n")

    # Install a temporary config with tight caps. Back up an existing
    # one so the user's real config is preserved.
    cfg_path = paths.config_dir() / config_mod.CONFIG_FILENAME
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    backup = cfg_path.read_text() if cfg_path.exists() else None
    cfg_path.write_text(
        "[subagents]\nmax_depth = 1\nmax_fanout = 1\n"
    )

    parent_session = Session(root=tmp / "sessions")
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
    terminate = subagent.make_terminate_subagent(state, agent)

    io_thread = threading.Thread(
        target=state.io_loop, name="test-io", daemon=True
    )
    io_thread.start()

    spawned_ids: list[str] = []
    try:
        # 1. fan-out cap: first spawn ok, second refused.
        first = spawn("a", "do nothing")
        assert not first.startswith("<"), f"first spawn failed: {first}"
        spawned_ids.append(first)
        print(f"✓ first spawn: {first}")

        second = spawn("b", "do nothing")
        assert second.startswith("<refused: at max_fanout=1"), second
        print(f"✓ second spawn refused: {second!r}")
        assert "b" not in [
            e.name for e in agent._subagents.values()
        ], "b should not be in registry after refusal"

        # Free the slot.
        terminate(first)
        # The process can take a moment to be reaped.
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and agent._subagents
        ):
            time.sleep(0.05)
        spawned_ids.remove(first)
        print(f"✓ slot freed via terminate")

        # 2. depth cap: simulate a depth-1 spawning agent (max_depth=1
        #    means depth+1 must be ≤ 1, so depth=1 → 2 is refused).
        agent.depth = 1
        refused = spawn("c", "do nothing")
        assert refused.startswith("<refused: would exceed max_depth=1"), refused
        print(f"✓ depth-cap refused: {refused!r}")
        agent.depth = 0
    finally:
        # Cleanup any survivors and restore config.
        for sid in list(spawned_ids):
            try:
                terminate(sid)
            except Exception:
                pass
        for sid, entry in list(agent._subagents.items()):
            try:
                entry.process.terminate()
                entry.process.join(timeout=2)
            except Exception:
                pass
        state.shutdown_event.set()
        io_thread.join(timeout=2)
        if backup is None:
            cfg_path.unlink(missing_ok=True)
        else:
            cfg_path.write_text(backup)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
