"""Unit smoke for tools.kill_active().

Spawns a `sleep 30` via execute() on a worker thread, fires
kill_active() from the main thread, and asserts the call returns
quickly with a non-zero exit code instead of waiting out the 60s
internal timeout.

Run with:

    .venv/bin/python -m tests.smoke_kill_active
"""

from __future__ import annotations

import threading
import time

from pyagent import tools as agent_tools
from pyagent import permissions


def main() -> None:
    # execute() does no permission gating today — it runs the command
    # directly. But pre-approve the cwd just in case any tool added
    # later does a path check.
    permissions.set_workspace(".")

    result: dict = {}

    def runner() -> None:
        result["start"] = time.monotonic()
        result["output"] = agent_tools.execute("sleep 30")
        result["end"] = time.monotonic()

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    # Wait for the subprocess to register itself, then kill it.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if agent_tools._ACTIVE_EXEC_PROCS:
            break
        time.sleep(0.01)
    assert agent_tools._ACTIVE_EXEC_PROCS, "execute did not register a proc"
    print(f"✓ active proc registered: pid={agent_tools._ACTIVE_EXEC_PROCS[0].pid}")

    killed = agent_tools.kill_active()
    assert killed == 1, f"expected 1 killed, got {killed}"
    print(f"✓ kill_active returned {killed}")

    t.join(timeout=5.0)
    assert not t.is_alive(), "execute did not return after kill"

    elapsed = result["end"] - result["start"]
    assert elapsed < 5.0, f"execute took {elapsed:.1f}s, expected sub-second"
    print(f"✓ execute returned in {elapsed:.2f}s (well under the 60s timeout)")

    output = result["output"]
    assert "exit_code" in output, output
    # SIGKILL = -9 in Python's returncode convention.
    assert "-9" in output.splitlines()[0], (
        f"expected SIGKILL exit code, got: {output.splitlines()[0]}"
    )
    print(f"✓ tool result reflects SIGKILL: {output.splitlines()[0]!r}")

    # Cleanup state.
    assert not agent_tools._ACTIVE_EXEC_PROCS, "proc not unregistered"
    print("✓ active list cleaned up")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
