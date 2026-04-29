"""End-to-end smoke for config-defined roles.

Covers the v1 roles surface:
  - Loading `[models.<name>]` from project-tier config (./.pyagent/config.toml)
  - `roles.resolve` precedence: role-name lookup wins, raw provider/model
    fallthrough, empty string returns ("", None)
  - `_build_subagent_config` carries role data (model override,
    role_body, role_tools, role_meta_tools) into the spawn config
  - End-to-end spawn-with-role: the subprocess boots with the role's
    model and processes a turn cleanly
  - `set_model` event handler swaps `agent.client` on success and
    leaves the existing client in place on failure

Run with:

    .venv/bin/python -m tests.smoke_roles
"""

from __future__ import annotations

import multiprocessing
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

from pyagent import agent_proc, paths, protocol, roles, subagent
from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient, LoremClient
from pyagent.session import Session


def _write_config(tmp: Path) -> None:
    (tmp / ".pyagent").mkdir(exist_ok=True)
    (tmp / ".pyagent" / "config.toml").write_text(
        """
[models.skim]
model = "pyagent/echo"
description = "Read-only quick-look."
system_prompt = "You are a skim agent — read, don't write."
tools = ["read_file", "grep"]
meta_tools = false

[models.cheap]
model = "pyagent/echo"
description = "Cheap and fast for narrow tasks."
"""
    )


def test_role_load_and_resolve(tmp: Path) -> None:
    loaded = roles.load()
    assert set(loaded) == {"skim", "cheap"}, loaded
    skim = loaded["skim"]
    assert skim.model == "pyagent/echo", skim.model
    assert skim.tools == ("read_file", "grep"), skim.tools
    assert skim.meta_tools is False, skim.meta_tools
    assert "skim agent" in skim.system_prompt, skim.system_prompt
    print("✓ role load: skim has tools allowlist + meta_tools=false")

    cheap = loaded["cheap"]
    assert cheap.tools is None, cheap.tools  # absent → inherit default set
    assert cheap.meta_tools is True, cheap.meta_tools
    print("✓ role load: cheap inherits default tools + meta_tools")

    # Precedence: role name wins
    m, role = roles.resolve("skim")
    assert role is not None and role.name == "skim", role
    assert m == "pyagent/echo", m
    print(f"✓ resolve('skim') → ({m}, role.name='skim')")

    # Raw provider/model string falls through
    m, role = roles.resolve("anthropic/claude-haiku-4-5")
    assert role is None, role
    assert m == "anthropic/claude-haiku-4-5", m
    print(f"✓ resolve('anthropic/claude-haiku-4-5') → ({m}, None)")

    # Empty signals "inherit parent"
    m, role = roles.resolve("")
    assert m == "" and role is None, (m, role)
    print("✓ resolve('') → ('', None) — caller inherits parent's model")


def test_build_subagent_config_with_role(tmp: Path) -> None:
    parent_session = MagicMock()
    parent_session.dir = tmp / "fake-session"
    parent_session.dir.mkdir(exist_ok=True)
    parent_session.id = "parent-1"

    base = {
        "cwd": str(tmp),
        "model": "pyagent/loremipsum",  # parent's model — should be overridden
        "soul_path": "x",
        "tools_path": "y",
        "primer_path": "z",
    }
    m, role = roles.resolve("skim")
    sid, cfg = subagent._build_subagent_config(
        name="peeker",
        system_prompt="Skim tools.py.",
        base_config=base,
        parent_session=parent_session,
        parent_depth=0,
        model_override=m,
        role=role,
    )
    assert cfg["model"] == "pyagent/echo", cfg["model"]
    assert cfg["task_body"] == "Skim tools.py.", cfg["task_body"]
    assert "skim agent" in cfg["role_body"], cfg["role_body"]
    assert cfg["role_tools"] == ["read_file", "grep"], cfg["role_tools"]
    assert cfg["role_meta_tools"] is False, cfg["role_meta_tools"]
    assert cfg["is_subagent"] is True
    assert sid.startswith("peeker-"), sid
    print(f"✓ _build_subagent_config: role data threaded into cfg ({sid})")


def test_register_tools_allowlist() -> None:
    a = Agent(client=EchoClient())
    agent_proc._register_tools(
        a, allow_meta=False, allowlist=["read_file", "grep"]
    )
    assert sorted(a.tools) == ["grep", "read_file"], sorted(a.tools)
    print("✓ _register_tools allowlist narrows registration")

    a2 = Agent(client=EchoClient())
    agent_proc._register_tools(a2, allow_meta=False, allowlist=None)
    # Default set: read/write file, list, grep, execute, fetch_url,
    # read/write ledger, read_skill (no meta).
    assert "execute" in a2.tools and "spawn_subagent" not in a2.tools
    assert len(a2.tools) == 9, sorted(a2.tools)
    print("✓ _register_tools allowlist=None → full default set, no meta")


def test_end_to_end_role_spawn(tmp: Path) -> None:
    """Spawn an actual subprocess subagent with a role and round-trip a turn."""
    soul = paths.resolve("SOUL.md", seed="SOUL.md")
    tools_md = paths.resolve("TOOLS.md", seed="TOOLS.md")
    primer = paths.resolve("PRIMER.md", seed="PRIMER.md")

    parent_session = Session(root=tmp / "sessions")

    ctx = multiprocessing.get_context("spawn")
    upstream_test_end, upstream_state_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=upstream_state_end)

    agent = Agent(
        client=EchoClient(),  # parent uses echo too — irrelevant here
        session=parent_session,
        depth=0,
    )

    base_config = {
        "cwd": str(tmp),
        "model": "pyagent/loremipsum",  # parent's model — role should override
        "soul_path": str(soul),
        "tools_path": str(tools_md),
        "primer_path": str(primer),
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
        # Spawn with role="skim" — the role's model (pyagent/echo) overrides
        # the parent's pyagent/loremipsum.
        sid = spawn("peeker", "Skim and report.", model="skim")
        assert not sid.startswith("<"), f"spawn failed: {sid}"
        entry = agent._subagents[sid]
        assert entry.process.is_alive(), "subagent died right after spawn"
        print(f"✓ spawned with role=skim: {sid}")

        reply = call(sid, "hello")
        # EchoClient just echoes the prompt back.
        assert reply == "hello", f"unexpected reply: {reply!r}"
        print(f"✓ role-spawned subagent round-tripped: {reply!r}")

        result = terminate(sid)
        assert "terminated" in result.lower(), result
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and entry.process.is_alive():
            time.sleep(0.05)
        assert not entry.process.is_alive(), "did not exit on terminate"
        print(f"✓ terminated: {result}")
    finally:
        for s_id, e in list(agent._subagents.items()):
            try:
                e.process.terminate()
                e.process.join(timeout=2)
            except Exception:
                pass
        state.shutdown_event.set()
        io_thread.join(timeout=2)


def test_set_model_handler() -> None:
    ctx = multiprocessing.get_context("spawn")
    parent_end, child_end = ctx.Pipe(duplex=True)
    state = agent_proc._ChildState(conn=child_end)
    state.agent = Agent(client=EchoClient())

    # Successful swap
    state._handle_set_model("pyagent/loremipsum")
    assert isinstance(state.agent.client, LoremClient), type(state.agent.client)
    print("✓ set_model: swapped EchoClient → LoremClient")

    # Bad spec leaves the existing client in place
    state._handle_set_model("nonsense/foo")
    assert isinstance(state.agent.client, LoremClient), (
        "bad set_model should not change the client"
    )
    print("✓ set_model: bad spec leaves client unchanged")

    # Drain events the handler emitted
    time.sleep(0.05)
    events = []
    while parent_end.poll():
        events.append(parent_end.recv())
    kinds = [(e.get("type"), e.get("level")) for e in events]
    assert ("info", "info") in kinds, kinds  # success
    assert ("info", "warn") in kinds, kinds   # failure
    print(f"✓ set_model: emitted info/warn events ({kinds})")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-roles-smoke-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")

    _write_config(tmp)

    test_role_load_and_resolve(tmp)
    test_build_subagent_config_with_role(tmp)
    test_register_tools_allowlist()
    test_end_to_end_role_spawn(tmp)
    test_set_model_handler()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
