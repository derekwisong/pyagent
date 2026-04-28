"""Smoke for permission_response routing root → subagent.

Drives `_ChildState._handle_parent_event` directly with a fake subagent
pipe and verifies that:
  1. A permission_response with agent_id=<sid> reaches the subagent's
     pipe.
  2. The agent_id field is stripped when forwarded — the subagent
     sees a normal event meant for itself, not one it would try to
     route to a non-existent grandchild.

No subprocess, no LLM. Run with:

    .venv/bin/python -m tests.smoke_subagent_routing
"""

from __future__ import annotations

import multiprocessing

from pyagent import agent_proc


def main() -> None:
    ctx = multiprocessing.get_context("spawn")
    parent_test_end, parent_state_end = ctx.Pipe(duplex=True)
    sub_state_end, sub_test_end = ctx.Pipe(duplex=True)

    # The state under test owns parent_state_end as its upstream pipe
    # and sub_state_end as the (registered) connection to a fake
    # subagent.
    state = agent_proc._ChildState(conn=parent_state_end)
    sid = "fake-subagent-deadbeef"
    state.register_subagent_pipe(sid, sub_state_end)

    # Simulate a permission_response from the CLI bound for the subagent.
    parent_test_end.send(
        {
            "type": "permission_response",
            "decision": True,
            "always": False,
            "agent_id": sid,
        }
    )

    # Drive one iteration of the parent-event handler.
    state._handle_parent_event()

    # The subagent should have received the event WITHOUT the agent_id.
    assert sub_test_end.poll(2.0), "subagent never got the forwarded event"
    forwarded = sub_test_end.recv()
    assert forwarded.get("type") == "permission_response", forwarded
    assert forwarded.get("decision") is True, forwarded
    assert forwarded.get("always") is False, forwarded
    assert "agent_id" not in forwarded, (
        f"agent_id should have been stripped; got {forwarded}"
    )
    print(f"✓ forwarded down stripped agent_id: {forwarded}")

    # An event with agent_id pointing at an unknown subagent should
    # be dropped silently (logged, not raised).
    parent_test_end.send(
        {
            "type": "permission_response",
            "decision": False,
            "always": False,
            "agent_id": "ghost-subagent",
        }
    )
    state._handle_parent_event()
    assert not sub_test_end.poll(0.2), (
        "ghost-targeted event should not have reached our real subagent"
    )
    print("✓ unknown agent_id targets are dropped silently")

    # An event with no agent_id should be treated as "for me".
    parent_test_end.send({"type": "cancel"})
    state._handle_parent_event()
    assert state.cancel_event.is_set(), "cancel should have set local event"
    print("✓ event without agent_id handled locally (cancel set)")

    # And cancel should have propagated to the subagent's pipe too.
    assert sub_test_end.poll(2.0), "cancel should propagate to subagent"
    cancel_ev = sub_test_end.recv()
    assert cancel_ev.get("type") == "cancel", cancel_ev
    assert "agent_id" not in cancel_ev, cancel_ev
    print(f"✓ cancel propagated to subagent: {cancel_ev}")

    # Cleanup.
    state.unregister_subagent_pipe(sid)
    parent_test_end.close()
    parent_state_end.close()
    sub_test_end.close()
    sub_state_end.close()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
