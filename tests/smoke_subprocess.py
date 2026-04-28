"""End-to-end smoke for the agent subprocess.

Spawns the child via the same path the CLI uses, drives it through the
existing `pyagent/echo` stub (no network), and asserts the protocol
round-trips cleanly.

The permission marshaling and cancel paths are unit-tested separately
in tests.smoke_permission_handler — exercising them end-to-end would
require a richer stub LLM than ships in the package.

Run with:

    .venv/bin/python -m tests.smoke_subprocess
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import time
from pathlib import Path


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    from pyagent import agent_proc, paths as paths_mod, protocol
    from pyagent.session import Session

    config_dir = paths_mod.config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    soul = config_dir / "SOUL.md"
    tools_md = config_dir / "TOOLS.md"
    primer = config_dir / "PRIMER.md"
    user_md = config_dir / "USER.md"
    for p, content in (
        (soul, "# soul\nbe helpful"),
        (tools_md, "# tools\n"),
        (primer, "# primer\n"),
        (user_md, "# user\n"),
    ):
        if not p.exists():
            p.write_text(content)

    session = Session()
    config = {
        "cwd": str(Path.cwd().resolve()),
        "model": "pyagent/echo",
        "session_id": session.id,
        "soul_path": str(soul),
        "tools_path": str(tools_md),
        "primer_path": str(primer),
        "approved_paths": [str(config_dir)],
    }

    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=True)
    proc = ctx.Process(
        target=agent_proc.child_main,
        args=(config, child_conn),
        name="pyagent-smoke-agent",
        daemon=True,
    )
    proc.start()
    child_conn.close()

    def _recv(timeout: float = 10.0) -> dict:
        if not parent_conn.poll(timeout):
            raise TimeoutError(f"no event in {timeout}s")
        return parent_conn.recv()

    try:
        # 1. ready handshake
        ev = _recv(15.0)
        assert ev["type"] == "ready", f"expected ready, got {ev}"
        print(f"✓ ready: {ev}")

        # 2. one prompt → assistant_text (echo) → turn_complete
        protocol.send(parent_conn, "user_prompt", prompt="hello agent")
        seen: list[str] = []
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            ev = _recv(5.0)
            seen.append(ev["type"])
            if ev["type"] == "assistant_text":
                assert ev["text"] == "hello agent", ev
                print(f"  ↩ echoed: {ev['text']!r}")
            elif ev["type"] == "turn_complete":
                break
            elif ev["type"] == "agent_error":
                raise RuntimeError(f"agent_error: {ev}")
        assert "assistant_text" in seen, seen
        assert seen[-1] == "turn_complete", seen
        print(f"✓ turn round-trip: {seen}")

        # 3. session persistence: child should have written conversation.jsonl
        history = session.load_history()
        assert len(history) >= 2, f"expected ≥2 entries, got {history}"
        first = history[0]
        assert first.get("role") == "user", first
        assert first.get("content") == "hello agent", first
        print(f"✓ history persisted: {len(history)} entries")

        # 4. clean shutdown via shutdown event
        protocol.send(parent_conn, "shutdown")
        proc.join(timeout=5)
        assert not proc.is_alive(), "child did not exit on shutdown"
        assert proc.exitcode == 0, f"non-zero exit: {proc.exitcode}"
        print(f"✓ clean shutdown: exit={proc.exitcode}")
    finally:
        try:
            parent_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=2)

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
