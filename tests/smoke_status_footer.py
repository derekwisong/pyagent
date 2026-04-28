"""Smoke for the CLI status footer.

Drives `_update_agents_state` and `_render_status` directly with
synthetic events and asserts the rendered footer reflects current
multi-agent activity. No subprocess.

Run with:

    .venv/bin/python -m tests.smoke_status_footer
"""

from __future__ import annotations

import io

from rich.console import Console

from pyagent.cli import _render_status, _update_agents_state


def render_plain(markup: str) -> str:
    """Strip ANSI / styles from `_render_status` output for assertion."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(markup)
    return buf.getvalue().rstrip()


def main() -> None:
    agents: dict[str, dict[str, str]] = {"root": {"status": "thinking"}}

    # Single-agent: classic 'thinking…' rendering.
    out = render_plain(_render_status(agents))
    assert out == "thinking…", repr(out)
    print(f"✓ single-agent: {out!r}")

    # Spawned subagent: footer expands.
    _update_agents_state(
        agents,
        {
            "type": "info",
            "message": "spawned subagent lead (id=lead-abc12345, depth=1)",
        },
    )
    assert "lead-abc12345" in agents, agents
    out = render_plain(_render_status(agents))
    assert "root" in out and "lead-abc12345" in out, out
    assert "│" in out, out
    print(f"✓ spawn rendered: {out!r}")

    # Subagent ready bubbles up.
    _update_agents_state(
        agents,
        {"type": "ready", "agent_id": "lead-abc12345"},
    )
    assert agents["lead-abc12345"]["status"] == "ready", agents
    print(f"✓ ready: {agents['lead-abc12345']}")

    # Tool call by subagent updates its status.
    _update_agents_state(
        agents,
        {
            "type": "tool_call_started",
            "agent_id": "lead-abc12345",
            "name": "execute",
            "args": {"command": "sleep 1"},
        },
    )
    assert agents["lead-abc12345"]["status"] == "· execute", agents
    out = render_plain(_render_status(agents))
    assert "· execute" in out, out
    print(f"✓ tool_call: {out!r}")

    # Tool result resets to thinking.
    _update_agents_state(
        agents,
        {
            "type": "tool_result",
            "agent_id": "lead-abc12345",
            "name": "execute",
            "content": "exit_code: 0\n",
        },
    )
    assert agents["lead-abc12345"]["status"] == "thinking", agents

    # Spawn a second subagent to confirm multi-agent ordering.
    _update_agents_state(
        agents,
        {
            "type": "info",
            "message": "spawned subagent helper (id=helper-deadbeef, depth=2)",
        },
    )
    out = render_plain(_render_status(agents))
    # Three labels separated by │
    assert out.count("│") == 2, out
    assert "helper-deadbeef" in out, out
    print(f"✓ three agents: {out!r}")

    # Terminate the second subagent removes it.
    _update_agents_state(
        agents,
        {
            "type": "info",
            "message": "terminated subagent helper (id=helper-deadbeef)",
        },
    )
    assert "helper-deadbeef" not in agents, agents
    out = render_plain(_render_status(agents))
    assert "helper-deadbeef" not in out, out
    print(f"✓ terminate removed: {out!r}")

    # Agent error sets status to 'error'.
    _update_agents_state(
        agents,
        {
            "type": "agent_error",
            "agent_id": "lead-abc12345",
            "kind": "RuntimeError",
            "message": "boom",
        },
    )
    assert agents["lead-abc12345"]["status"] == "error", agents
    print(f"✓ error: {agents['lead-abc12345']}")

    # Unknown event types are ignored (no crash).
    _update_agents_state(
        agents, {"type": "weird_unknown_event_type"}
    )
    print("✓ unknown event ignored")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
