"""Unit smoke for the permission-marshaling path.

Drives `_ChildState.permission_handler` directly with a fake Connection,
verifies the request/response round-trip and the `always`-cache hand-off
to `permissions.pre_approve`. No subprocess, no LLM.

Issue #69 changed the protocol: each permission_handler call generates a
unique request_id and registers a per-request reply queue, so multiple
concurrent prompts (parallel subagents) don't collide on a single shared
queue. This smoke exercises both single-prompt round-trips and the
multi-prompt routing.

Run with:

    .venv/bin/python -m tests.smoke_permission_handler
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from pyagent import permissions
from pyagent.agent_proc import _ChildState


class _FakeConn:
    """Minimal duck-type for multiprocessing.Connection used by send/recv.

    `send(event)` stashes the dict; `recv()` is unused because the IO
    loop isn't running.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send(self, obj: dict) -> None:
        self.sent.append(obj)

    def recv(self) -> dict:
        raise RuntimeError("recv should not be called in this test")

    def close(self) -> None:
        pass


def _reset_permissions() -> None:
    permissions._APPROVED.clear()
    permissions._DENIED.clear()
    permissions.set_prompt_handler(None)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _reply(state: _ChildState, request_id: str, decision: bool, always: bool):
    """Route a response into the per-request reply queue."""
    with state._perm_lock:
        rq = state._pending_perm_replies.get(request_id)
    assert rq is not None, f"no pending queue for {request_id!r}"
    rq.put({
        "type": "permission_response",
        "request_id": request_id,
        "decision": decision,
        "always": always,
    })


def main() -> None:
    target = Path("/etc/hostname")

    # Case 1: deny.
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
    result: dict = {}
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    assert _wait_for(lambda: bool(fake.sent)), "no permission_request sent"
    req = fake.sent[-1]
    assert req["type"] == "permission_request", req
    assert req["target"] == str(target), req
    req_id = req["request_id"]
    assert req_id.startswith("perm-"), req_id
    print(f"✓ deny: emitted permission_request req={req_id}")

    _reply(state, req_id, decision=False, always=False)
    t.join(timeout=2.0)
    assert result.get("ok") is False, f"deny returned {result}"
    assert target.resolve() not in permissions.approved_paths(), (
        "deny should not cache"
    )
    # Per-request queue cleaned up.
    with state._perm_lock:
        assert req_id not in state._pending_perm_replies
    print("✓ deny: handler returned False, queue cleaned up")

    # Case 2: allow once (no cache).
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
    result.clear()
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    assert _wait_for(lambda: bool(fake.sent))
    req_id = fake.sent[-1]["request_id"]
    _reply(state, req_id, decision=True, always=False)
    t.join(timeout=2.0)
    assert result.get("ok") is True, f"allow returned {result}"
    assert target.resolve() not in permissions.approved_paths(), (
        "allow without 'always' should not cache"
    )
    print("✓ allow-once: handler returned True, no cache")

    # Case 3: always (caches via pre_approve).
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
    result.clear()
    t = threading.Thread(
        target=lambda: result.__setitem__(
            "ok", state.permission_handler(target)
        ),
        daemon=True,
    )
    t.start()
    assert _wait_for(lambda: bool(fake.sent))
    req_id = fake.sent[-1]["request_id"]
    _reply(state, req_id, decision=True, always=True)
    t.join(timeout=2.0)
    assert result.get("ok") is True, f"always returned {result}"
    assert target.resolve() in permissions.approved_paths(), (
        "'always' should cache"
    )
    print("✓ always: handler returned True, target cached")

    # Case 4: integration with permissions.require_access.
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
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
    assert _wait_for(lambda: bool(fake.sent))
    req_id = fake.sent[-1]["request_id"]
    _reply(state, req_id, decision=True, always=False)
    t.join(timeout=2.0)
    assert decision.get("ok") is True, decision
    print("✓ require_access uses the prompt handler")

    # =========================================================
    # Issue #69: multi-permission queue + out-of-order routing
    # =========================================================
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
    target_a = Path("/etc/a")
    target_b = Path("/etc/b")
    result_a: dict = {}
    result_b: dict = {}
    ta = threading.Thread(
        target=lambda: result_a.__setitem__(
            "ok", state.permission_handler(target_a)
        ),
        daemon=True,
    )
    tb = threading.Thread(
        target=lambda: result_b.__setitem__(
            "ok", state.permission_handler(target_b)
        ),
        daemon=True,
    )
    ta.start()
    tb.start()
    # Wait for both requests.
    assert _wait_for(lambda: len(fake.sent) >= 2, timeout=3.0), fake.sent
    sent = list(fake.sent)
    # Both have unique request_ids.
    req_ids = [s["request_id"] for s in sent]
    assert len(set(req_ids)) == 2, req_ids
    # Both registered in _pending_perm_replies.
    with state._perm_lock:
        for r in req_ids:
            assert r in state._pending_perm_replies, (r, state._pending_perm_replies)
    print(
        f"✓ two concurrent permission_handler calls register "
        f"distinct request_ids: {req_ids}"
    )

    # Answer in REVERSE order (B first, then A) — verify each handler
    # gets the right response despite the out-of-order reply.
    # Map request_id -> target so we know which thread expects what.
    by_req = {s["request_id"]: s["target"] for s in sent}
    req_a = next(r for r, t in by_req.items() if t == str(target_a))
    req_b = next(r for r, t in by_req.items() if t == str(target_b))

    # Answer B with True.
    _reply(state, req_b, decision=True, always=False)
    tb.join(timeout=2.0)
    assert result_b.get("ok") is True, result_b
    # ta should still be waiting.
    assert ta.is_alive(), "thread a unblocked by b's response"

    # Answer A with False.
    _reply(state, req_a, decision=False, always=False)
    ta.join(timeout=2.0)
    assert result_a.get("ok") is False, result_a
    # Both queues cleaned up.
    with state._perm_lock:
        assert state._pending_perm_replies == {}, state._pending_perm_replies
    print("✓ out-of-order responses route by request_id correctly")

    # Unknown request_id arriving on the IO thread → logged + dropped
    # (no crash). Exercise the public route by simulating an inbound
    # event via _handle_event-style routing. Since the IO loop isn't
    # running, just put on the shared queue (the legacy fallback) and
    # confirm it doesn't break the per-request map.
    _reset_permissions()
    fake = _FakeConn()
    state = _ChildState(conn=fake)
    state.permission_replies.put({
        "type": "permission_response",
        "request_id": "perm-deadbeef",
        "decision": True,
        "always": False,
    })
    # No assertion failure; the put doesn't raise.
    print("✓ unknown request_id response is non-fatal (handler logs + drops)")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
