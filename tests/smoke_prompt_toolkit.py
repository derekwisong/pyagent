"""Smoke for the prompt_toolkit-backed REPL input.

Runs pyagent under a real PTY so prompt_toolkit's interactive path
is exercised (the PIPE-based smoke_ctrlc falls back to a non-tty
input mode and wouldn't catch a regression here). Asserts:

  1. The CLI starts cleanly, the agent reaches `ready`, and the `> `
     prompt is rendered.
  2. EOF (Ctrl-D) at the prompt cleanly exits the CLI — same
     behavior as the readline-backed `input()` we replaced.
  3. No Python traceback in the captured output.

Uses the `pyagent/echo` stub so no network is required.

Run with:

    .venv/bin/python -m tests.smoke_prompt_toolkit
"""

from __future__ import annotations

import os
import pty
import select
import sys
import tempfile
import time
from pathlib import Path


def _read_until(fd: int, needle: bytes, timeout_s: float) -> bytes:
    """Read from `fd` until `needle` appears or timeout. Returns the
    accumulated bytes regardless of whether the needle was found —
    caller asserts."""
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        r, _, _ = select.select([fd], [], [], min(remaining, 0.2))
        if fd in r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf.extend(chunk)
            if needle in buf:
                return bytes(buf)
    return bytes(buf)


def main() -> None:
    repo_root = Path(__file__).parent.parent
    pyagent_bin = repo_root / ".venv" / "bin" / "pyagent"
    if not pyagent_bin.exists():
        print(f"skip: {pyagent_bin} not present")
        return

    work = Path(tempfile.mkdtemp(prefix="pyagent-pty-"))

    pid, fd = pty.fork()
    if pid == 0:
        # Child — exec pyagent in the temp work dir.
        os.chdir(str(work))
        os.execv(
            str(pyagent_bin),
            [str(pyagent_bin), "--model", "pyagent/echo"],
        )
        # unreachable
        os._exit(127)

    try:
        # Wait for the `>` prompt char to render. prompt_toolkit
        # positions the cursor with escape sequences after the
        # prompt, so we don't see a literal "> " — just the `>`
        # bracketed by ANSI cursor moves. Presence of the `>` plus
        # the session header is enough to know the REPL is live.
        out = _read_until(fd, b">", timeout_s=10.0)
        assert b"session:" in out, f"no session header:\n{out!r}"
        assert b">" in out, f"no prompt rendered:\n{out!r}"
        print("✓ ready, prompt rendered under PTY")

        # Send EOF (Ctrl-D, byte 0x04) — prompt_toolkit should turn
        # this into EOFError, which the main loop catches and exits.
        os.write(fd, b"\x04")

        # Drain any remaining output (the resume hint, etc.).
        deadline = time.monotonic() + 10.0
        tail = bytearray()
        while time.monotonic() < deadline:
            r, _, _ = select.select([fd], [], [], 0.2)
            if fd in r:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                tail.extend(chunk)
            else:
                # Quiescent — check if child has exited.
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    break
        else:
            os.kill(pid, 9)
            raise AssertionError("CLI did not exit within 10s of EOF")

        # Reap if not already reaped.
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            wpid, status = pid, 0

        combined = bytes(out + bytes(tail))
        assert b"Traceback" not in combined, (
            f"traceback in PTY output:\n{combined!r}"
        )
        print("✓ EOF cleanly exited the CLI; no traceback")

        # Exit status: 0 is the only acceptable result for a clean EOF.
        if os.WIFEXITED(status):
            rc = os.WEXITSTATUS(status)
            assert rc == 0, f"unexpected exit code {rc}"
            print(f"✓ exit code: {rc}")
        elif os.WIFSIGNALED(status):
            raise AssertionError(
                f"CLI was killed by signal {os.WTERMSIG(status)}"
            )

        print("\nALL CHECKS PASSED")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == "__main__":
    main()
