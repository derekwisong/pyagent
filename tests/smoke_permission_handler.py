"""Unit smoke for the permission-marshaling path.

Drives `_ChildState.permission_handler` directly with a fake Connection,
verifies the request/response round-trip and the `always`-cache hand-off
to `permissions.pre_approve`. No subprocess, no LLM.

Run with:

    .venv/bin/python -m tests.smoke_permission_handler
"""

from __future__ import annotations

import threading
from pathlib import Path

from pyagent import permissions
from pyagent.agent_proc import _ChildState


class _FakeConn:
    """Minimal duck-type for multiprocessing.Connection used by send/recv.

    `send(event)` stashes the dict; `recv()` is unused in this test
    because the IO loop isn't running. The handler only uses send.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, obj: dict) -> None:
        self.sent.append(obj)

    def recv(self) -> dict:  # unused in this test
        raise RuntimeError("recv should not be called in this test")

    def close(self) -> None:
        pass


def _reset_permissions() -> None:
    # Best-effort reset of module-level state between cases.
    permissions._APPROVED.clear()
    permissions._DENIED.clear()
    permissions.set_prompt_handler(None)


def main() -> None:
    fake = _FakeConn()
    state = _ChildState(conn=fake)

    # The handler blocks on the replies queue, so we drive it on a
    # background thread and post a reply from the main thread.
    target = Path("/etc/hostname")

    # Case 1: deny.
    _reset_permissions()
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    # Wait for the request to be sent.
    deadline = 0
    import time

    end = time.monotonic() + 2.0
    while time.monotonic() < end and not fake.sent:
        time.sleep(0.01)
    assert fake.sent, "handler did not send permission_request"
    req = fake.sent[-1]
    assert req["type"] == "permission_request", req
    assert req["target"] == str(target), req
    print(f"✓ deny: emitted {req}")

    state.permission_replies.put(
        {"type": "permission_response", "decision": False, "always": False}
    )
    t.join(timeout=2.0)
    assert result.get("ok") is False, f"deny path returned {result}"
    assert target.resolve() not in permissions.approved_paths(), (
        "deny should not cache"
    )
    print("✓ deny: handler returned False, no cache")

    # Case 2: allow once (no cache).
    _reset_permissions()
    fake.sent.clear()
    result.clear()
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    end = time.monotonic() + 2.0
    while time.monotonic() < end and not fake.sent:
        time.sleep(0.01)
    state.permission_replies.put(
        {"type": "permission_response", "decision": True, "always": False}
    )
    t.join(timeout=2.0)
    assert result.get("ok") is True, f"allow path returned {result}"
    assert target.resolve() not in permissions.approved_paths(), (
        "allow without 'always' should not cache"
    )
    print("✓ allow-once: handler returned True, no cache")

    # Case 3: always (caches via pre_approve).
    _reset_permissions()
    fake.sent.clear()
    result.clear()
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    end = time.monotonic() + 2.0
    while time.monotonic() < end and not fake.sent:
        time.sleep(0.01)
    state.permission_replies.put(
        {"type": "permission_response", "decision": True, "always": True}
    )
    t.join(timeout=2.0)
    assert result.get("ok") is True, f"always path returned {result}"
    assert target.resolve() in permissions.approved_paths(), (
        "'always' should cache"
    )
    print("✓ always: handler returned True, target cached")

    # Case 4: integration with permissions.require_access.
    # After installing the handler, an out-of-workspace path should
    # route through it, not stdin.
    _reset_permissions()
    fake.sent.clear()
    permissions.set_workspace("/tmp")
    permissions.set_prompt_handler(state.permission_handler)
    outside = Path("/etc/hostname")

    decision: dict = {}
    t = threading.Thread(
        target=lambda: decision.__setitem__(
            "ok", permissions.require_access(outside)
        ),
        daemon=True,
    )
    t.start()
    end = time.monotonic() + 2.0
    while time.monotonic() < end and not fake.sent:
        time.sleep(0.01)
    assert fake.sent, "require_access did not invoke handler"
    state.permission_replies.put(
        {"type": "permission_response", "decision": True, "always": False}
    )
    t.join(timeout=2.0)
    assert decision.get("ok") is True, decision
    print("✓ require_access uses the prompt handler")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
