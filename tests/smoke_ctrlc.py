"""Smoke for clean Ctrl+C behavior.

Subprocess-launches the full `pyagent` CLI in its own process group
(simulating an interactive terminal), waits for the agent to become
ready, then sends SIGINT to the entire group — exactly what a real
Ctrl+C does in a terminal. Asserts:

  1. The CLI exits within a reasonable timeout (no hang).
  2. Neither stdout nor stderr contains a `Traceback (most recent…)`
     line. Both the CLI's main thread and the agent subprocess have
     to handle SIGINT gracefully — the agent because it shares the
     process group, the CLI because Python's default SIGINT raises
     KeyboardInterrupt at whatever the main thread is doing.

Uses the `pyagent/echo` stub LLM so no network is required.

Run with:

    .venv/bin/python -m tests.smoke_ctrlc
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).parent.parent
    pyagent_bin = repo_root / ".venv" / "bin" / "pyagent"
    if not pyagent_bin.exists():
        print(f"skip: {pyagent_bin} not present")
        return

    # Run in a fresh tmp dir so the .pyagent/sessions/ scribbling
    # doesn't pollute the repo.
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="pyagent-ctrlc-"))

    proc = subprocess.Popen(
        [
            str(pyagent_bin),
            "--model",
            "pyagent/echo",
        ],
        cwd=str(work),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group
    )

    # Wait long enough for the child agent to bootstrap and the CLI
    # to print its session/model header. 3s is generous on a healthy
    # box; a real test would parse stdout for "session:" but we want
    # to test the path where the CLI is mid-input AND a path where
    # SIGINT lands during ready-wait or shortly after.
    time.sleep(3)

    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGINT)

    try:
        out, err = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("pyagent did not exit within 15s of Ctrl+C")

    print(f"exit code: {proc.returncode}")
    print(f"--- stdout ---\n{out}")
    print(f"--- stderr ---\n{err}")

    # The actual assertions: no Python tracebacks in either stream.
    assert "Traceback" not in out, f"traceback in stdout:\n{out}"
    assert "Traceback" not in err, f"traceback in stderr:\n{err}"
    print("✓ no traceback in either stream")

    # Reasonable exit code: 0 (clean exit) is ideal. 130 (128 + SIGINT)
    # is acceptable if the shell gave up before our handler ran.
    # Anything else suggests a crash.
    assert proc.returncode in (0, 130), (
        f"unexpected exit code {proc.returncode}"
    )
    print(f"✓ acceptable exit code: {proc.returncode}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
