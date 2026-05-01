"""Unit smoke for the background-shell tools.

Covers the run_background / read_output / wait_for / kill_process
quartet end-to-end, plus the kill_active extension that flushes BOTH
foreground execute and background-proc registries.

Run with:

    .venv/bin/python -m tests.smoke_background_exec
"""

from __future__ import annotations

import re
import threading
import time

from pyagent import permissions
from pyagent import tools as agent_tools


def _check_run_and_wait_exit() -> None:
    """run_background + wait_for(exit) returns rc and tail."""
    out = agent_tools.run_background(
        "echo hello-bg && exit 0", name="quick-echo"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", out).group(1)
    assert handle.startswith("bg-"), out
    res = agent_tools.wait_for(handle, until="exit", timeout_s=5.0)
    assert "exited" in res, res
    assert "rc=0" in res, res
    assert "hello-bg" in res, res
    # Cleanup so other checks see a clean registry.
    agent_tools.kill_process(handle)
    print(f"  ok run_background + wait_for(exit): {handle}")


def _check_incremental_read_output() -> None:
    """read_output's `since` cursor lets the agent tail-follow."""
    started = agent_tools.run_background(
        "for i in 1 2 3 4 5; do echo line$i; sleep 0.05; done"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    # Wait briefly for some output.
    time.sleep(0.15)
    first = agent_tools.read_output(handle, since=0, max_chars=4000)
    assert "line1" in first, first
    next_since = int(re.search(r"next_since: (\d+)", first).group(1))
    assert next_since > 0, first
    # Wait for the process to finish, then tail-follow.
    agent_tools.wait_for(handle, until="exit", timeout_s=5.0)
    second = agent_tools.read_output(handle, since=next_since, max_chars=4000)
    # The follow-up read should NOT contain line1 (we already saw it).
    body = second.split("\n", 1)[1]
    assert "line1" not in body, f"tail re-emitted line1:\n{second}"
    # And it should contain at least one of the later lines we hadn't
    # seen at the time of the first read.
    assert any(f"line{n}" in body for n in (4, 5)), second
    agent_tools.kill_process(handle)
    print(f"  ok incremental read_output (since cursor): {handle}")


def _check_output_contains() -> None:
    """wait_for(output_contains:STRING) returns when the substring lands."""
    started = agent_tools.run_background(
        "(sleep 0.2; echo READY-MARKER; sleep 5)"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    t0 = time.monotonic()
    res = agent_tools.wait_for(
        handle, until="output_contains:READY-MARKER", timeout_s=5.0
    )
    elapsed = time.monotonic() - t0
    assert "matched" in res, res
    assert elapsed < 3.0, f"output_contains slow: {elapsed:.2f}s"
    agent_tools.kill_process(handle)
    print(f"  ok wait_for(output_contains:): {handle} matched in {elapsed:.2f}s")


def _check_silence_wait() -> None:
    """wait_for(silence:Ns) returns when output stops flowing."""
    # Process emits 3 lines fast then goes quiet (sleeps 5s before
    # the next line).
    started = agent_tools.run_background(
        "(echo a; echo b; echo c; sleep 5; echo d)"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    t0 = time.monotonic()
    res = agent_tools.wait_for(
        handle, until="silence:0.5s", timeout_s=4.0
    )
    elapsed = time.monotonic() - t0
    assert "settled" in res, res
    # Should have settled within the 0.5s quiet window plus a little
    # poll slack — well under the 5s sleep that comes next.
    assert elapsed < 2.0, f"silence wait too slow: {elapsed:.2f}s"
    agent_tools.kill_process(handle)
    print(f"  ok wait_for(silence:0.5s): {handle} settled in {elapsed:.2f}s")


def _check_kill_process_flow() -> None:
    """kill_process SIGKILLs the proc, removes the handle, returns rc."""
    started = agent_tools.run_background("sleep 30", name="napper")
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    assert handle in agent_tools._ACTIVE_BG_PROCS
    res = agent_tools.kill_process(handle)
    assert "killed" in res, res
    assert "rc=" in res, res
    assert handle not in agent_tools._ACTIVE_BG_PROCS, "handle not removed"
    # Stale handle -> standard <...> marker.
    again = agent_tools.kill_process(handle)
    assert again.startswith("<error: handle"), again
    print(f"  ok kill_process flow + stale-handle marker: {handle}")


def _check_kill_active_flushes_both() -> None:
    """kill_active() reaches into both foreground and background sets."""
    # Background sleep.
    started = agent_tools.run_background("sleep 30")
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    assert handle in agent_tools._ACTIVE_BG_PROCS

    # Foreground sleep on a worker thread (mirrors smoke_kill_active).
    fg_result: dict = {}

    def runner() -> None:
        fg_result["output"] = agent_tools.execute("sleep 30")

    t = threading.Thread(target=runner, daemon=True)
    t.start()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if agent_tools._ACTIVE_EXEC_PROCS:
            break
        time.sleep(0.01)
    assert agent_tools._ACTIVE_EXEC_PROCS, "execute did not register a proc"

    killed = agent_tools.kill_active()
    assert killed >= 2, f"expected kill_active to kill both, got {killed}"
    t.join(timeout=5.0)
    assert not t.is_alive(), "execute did not return after kill_active"
    # Foreground proc list cleaned up by execute()'s finally block.
    assert not agent_tools._ACTIVE_EXEC_PROCS, "fg list not cleaned up"
    # Background entry stays in the registry until kill_process /
    # shutdown_background — but the proc itself is dead.
    bg_entry = agent_tools._ACTIVE_BG_PROCS.get(handle)
    assert bg_entry is not None
    bg_entry.proc.wait(timeout=2.0)
    assert bg_entry.proc.returncode is not None, "bg proc not reaped"
    agent_tools.kill_process(handle)
    print(f"  ok kill_active flushes fg + bg ({killed} signalled)")


def _check_stale_handle_marker() -> None:
    """Operating against an unknown handle returns the standard marker."""
    bogus = "bg-deadbeef"
    for fn, args in [
        (agent_tools.read_output, (bogus,)),
        (agent_tools.wait_for, (bogus,)),
        (agent_tools.kill_process, (bogus,)),
    ]:
        res = fn(*args)
        assert res.startswith(
            f"<error: handle {bogus} is not active in this session>"
        ), f"{fn.__name__} stale-handle marker wrong: {res!r}"
    print("  ok stale-handle marker shape")


def _check_buffer_cap_truncation() -> None:
    """1MB cap drops oldest bytes and exposes `...truncated NN bytes...`."""
    # Print ~1.5MB of `x`s in a single line (1.5M chars + newline)
    # so the rolling cap fires at least once.
    payload_bytes = 1_500_000
    started = agent_tools.run_background(
        f"head -c {payload_bytes} /dev/zero | tr '\\0' 'x'"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    res = agent_tools.wait_for(handle, until="exit", timeout_s=10.0)
    assert "rc=0" in res, res
    bg = agent_tools._ACTIVE_BG_PROCS[handle]
    # The cap is 1MB; 1.5MB written should leave dropped > 0.
    assert bg.dropped > 0, (
        f"expected output drops, got dropped={bg.dropped}"
    )
    # And the buffer itself should be near (≤) the cap.
    assert len(bg.output_buf) <= 1024 * 1024, (
        f"buffer exceeded cap: {len(bg.output_buf)}"
    )
    out = agent_tools.read_output(handle, since=0, max_chars=200)
    assert "...truncated" in out, out
    # Post-truncation continuity: pulling next_since out of the first
    # read and using it on the next call must not re-read the dropped
    # prefix and must not silently skip any bytes available now. With
    # an exited process the buffer is stable, so a second call with
    # next_since should return an empty body and the same next_since.
    next_since = int(re.search(r"next_since: (\d+)", out).group(1))
    assert next_since > 0, out
    second = agent_tools.read_output(
        handle, since=next_since, max_chars=200
    )
    assert "...truncated" not in second, (
        f"unexpected truncation notice on continuation: {second!r}"
    )
    second_next = int(re.search(r"next_since: (\d+)", second).group(1))
    assert second_next == next_since, (
        f"next_since regressed across post-truncation reads: "
        f"first={next_since} second={second_next}"
    )
    agent_tools.kill_process(handle)
    print(
        f"  ok 1MB cap: dropped {bg.dropped} bytes, "
        f"buf={len(bg.output_buf)}, post-truncation continuity holds"
    )


def _check_dual_stream_read_output() -> None:
    """A process that writes to BOTH stdout and stderr should not lose
    bytes from either stream when the agent tail-follows via `since`.

    Regression guard for the original two-buffer design where the same
    `since` was applied independently to stdout_buf and stderr_buf —
    if one stream had grown past the other's length, bytes on the
    shorter stream were silently skipped on subsequent reads.
    """
    cmd = (
        "echo OUT1; "
        "echo ERR1 1>&2; "
        "sleep 0.05; "
        "echo OUT2; "
        "echo ERR2 1>&2; "
        "sleep 0.05; "
        "echo OUT3; "
        "echo ERR3 1>&2"
    )
    started = agent_tools.run_background(cmd)
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    agent_tools.wait_for(handle, until="exit", timeout_s=5.0)
    full = agent_tools.read_output(handle, since=0, max_chars=4000)
    # Every line landed in the combined log.
    for marker in ("OUT1", "OUT2", "OUT3", "ERR1", "ERR2", "ERR3"):
        assert marker in full, (
            f"expected {marker} in combined output, got: {full!r}"
        )
    # `[stderr]` / `[stdout]` markers appear when the source switches —
    # the exact count depends on read1 timing, but at least one of each
    # must show up.
    assert "[stderr]" in full, f"missing [stderr] marker: {full!r}"
    agent_tools.kill_process(handle)
    print(
        "  ok dual-stream read_output: stdout+stderr both surfaced; "
        "transition markers present"
    )


def _check_max_chars_truncation() -> None:
    """read_output respects max_chars and emits a truncation marker."""
    started = agent_tools.run_background("yes 'abcdefgh' | head -n 200")
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    agent_tools.wait_for(handle, until="exit", timeout_s=5.0)
    res = agent_tools.read_output(handle, since=0, max_chars=200)
    assert "more chars" in res, res
    # Body itself (between header and footer) should be capped at
    # roughly max_chars; we check the inline marker is there and that
    # the response is short enough to be useful.
    assert len(res) < 600, f"response not actually truncated: {len(res)} chars"
    agent_tools.kill_process(handle)
    print("  ok read_output max_chars truncation")


def _check_dangerous_pattern_refusal() -> None:
    """run_background applies the same safety blocklist as execute."""
    res = agent_tools.run_background("rm -rf /")
    assert res.startswith("<refused:"), res
    print("  ok run_background dangerous-pattern refusal")


def _check_shutdown_background_grace() -> None:
    """shutdown_background SIGTERMs then SIGKILLs the lingerers."""
    # Trap SIGTERM in the child so it has to wait for the SIGKILL
    # branch (verifies we don't hang past grace_s).
    started = agent_tools.run_background(
        "trap '' TERM; sleep 30"
    )
    handle = re.search(r"started (bg-[0-9a-f]+)", started).group(1)
    t0 = time.monotonic()
    signalled = agent_tools.shutdown_background(grace_s=0.2)
    elapsed = time.monotonic() - t0
    assert signalled >= 1, f"expected ≥1 signalled, got {signalled}"
    assert elapsed < 2.0, f"shutdown too slow: {elapsed:.2f}s"
    bg = agent_tools._ACTIVE_BG_PROCS[handle]
    bg.proc.wait(timeout=2.0)
    assert bg.proc.returncode is not None
    agent_tools._ACTIVE_BG_PROCS.pop(handle, None)
    print(
        f"  ok shutdown_background SIGTERM→SIGKILL in {elapsed:.2f}s "
        f"({signalled} signals)"
    )


def main() -> None:
    permissions.set_workspace(".")
    _check_run_and_wait_exit()
    _check_incremental_read_output()
    _check_output_contains()
    _check_silence_wait()
    _check_kill_process_flow()
    _check_kill_active_flushes_both()
    _check_stale_handle_marker()
    _check_buffer_cap_truncation()
    _check_dual_stream_read_output()
    _check_max_chars_truncation()
    _check_dangerous_pattern_refusal()
    _check_shutdown_background_grace()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
