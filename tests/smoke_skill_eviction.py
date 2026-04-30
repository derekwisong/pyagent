"""Smoke for `read_skill` body eviction (issue #10).

Locks six behaviors of `Agent._apply_eviction` and the
`evict_after_use=True` flag on `Agent.add_tool`:

  1. Default `evict_after_use=False` — result content stays in
     conversation across many turns.
  2. `evict_after_use=True` — result content is replaced with the
     stub once the NEXT assistant turn produces output.
  3. The most recent eviction-flagged result (no following
     assistant turn yet) is preserved — load-bearing.
  4. Idempotence — running `_apply_eviction` twice doesn't
     double-stub; the second run reports zero new evictions.
  5. Resume — `Session.load_history` returns the full content
     (JSONL untouched), and a fresh Agent applying the same
     eviction pass produces the in-memory stubs without mutating
     the file on disk.
  6. Multiple skills loaded in sequence — every consumed skill
     gets evicted at the right turn (not just the first one).

Run with:

    .venv/bin/python -m tests.smoke_skill_eviction
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient
from pyagent.session import Session


def _user(content: str) -> dict:
    return {"role": "user", "content": content}


def _assistant(text: str = "", tool_calls: list | None = None) -> dict:
    return {
        "role": "assistant",
        "text": text,
        "tool_calls": tool_calls or [],
    }


def _tool_results(results: list[dict]) -> dict:
    return {"role": "user", "tool_results": results}


def _make_agent(*, evict_skill: bool, evict_other: bool = False) -> Agent:
    """Build an Agent with stubbed tools registered for the test.

    The tool callables are never invoked here — we synthesize the
    conversation directly to exercise the eviction walk.
    """
    agent = Agent(client=EchoClient())

    def _read_skill(name: str = "x") -> str:  # pragma: no cover (not invoked)
        return f"body of {name}"

    def _grep(pattern: str = "x") -> str:  # pragma: no cover (not invoked)
        return f"matches for {pattern}"

    agent.add_tool(
        "read_skill", _read_skill,
        auto_offload=False, evict_after_use=evict_skill,
    )
    agent.add_tool("grep", _grep, evict_after_use=evict_other)
    return agent


def _check_default_no_eviction() -> None:
    """Tools without the flag never lose their result content."""
    agent = _make_agent(evict_skill=False, evict_other=False)
    grep_payload = "alpha\nbeta\ngamma"
    agent.conversation = [
        _user("hello"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "grep", "args": {"pattern": "x"}}
        ]),
        _tool_results([{"id": "c1", "name": "grep", "content": grep_payload}]),
        _assistant(text="found stuff"),
        _user("more"),
        _assistant(text="ok"),
        _user("again"),
        _assistant(text="still ok"),
    ]

    n = agent._apply_eviction()
    assert n == 0, f"unexpected eviction with no flagged tools: {n}"
    # The grep result content should still be the original payload
    # several assistant turns later.
    found = agent.conversation[2]["tool_results"][0]["content"]
    assert found == grep_payload, (
        f"non-evictable content was mutated: {found!r}"
    )
    print("✓ default flag (False) keeps content across many turns")


def _check_evict_after_next_assistant_turn() -> None:
    """A flagged result is stubbed once the next assistant turn produces output."""
    agent = _make_agent(evict_skill=True)
    body = "Skill body: long reference content " * 100  # ~3.5KB
    agent.conversation = [
        _user("look up the skill"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "read_skill", "args": {"name": "foo"}}
        ]),
        _tool_results([
            {"id": "c1", "name": "read_skill", "content": body}
        ]),
        # First consuming assistant turn — produces text.
        _assistant(text="OK, I read the skill. Running it now."),
    ]

    # No FOLLOWING assistant turn after the consumer yet — wait, the
    # consumer IS the only assistant-with-output AFTER the result, so
    # it makes the result stale. Walk: assistant_with_output indices
    # = [3]. The tool_results entry is at index 2, and 2 < 3, so it
    # IS evicted.
    n = agent._apply_eviction()
    assert n == 1, f"expected exactly 1 eviction, got {n}"
    stub = agent.conversation[2]["tool_results"][0]["content"]
    assert "evicted to save context" in stub, stub
    assert "'read_skill'" in stub, stub
    assert body not in stub, "raw body leaked into stub"
    print("✓ flagged result stubbed after a consuming assistant turn")


def _check_most_recent_preserved() -> None:
    """The most recent flagged result is load-bearing — never stubbed yet."""
    agent = _make_agent(evict_skill=True)
    body_1 = "skill one body " * 50
    body_2 = "skill two body " * 50
    agent.conversation = [
        _user("read first skill"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "read_skill", "args": {"name": "foo"}}
        ]),
        _tool_results([
            {"id": "c1", "name": "read_skill", "content": body_1}
        ]),
        _assistant(text="now read second"),  # consumes body_1
        _user("ok"),
        _assistant(text="", tool_calls=[
            {"id": "c2", "name": "read_skill", "args": {"name": "bar"}}
        ]),
        _tool_results([
            {"id": "c2", "name": "read_skill", "content": body_2}
        ]),
        # No assistant turn AFTER the second tool_results — the
        # second result is the most recent and must be preserved.
    ]

    n = agent._apply_eviction()
    assert n == 1, f"expected 1 eviction (body_1 only), got {n}"

    first_content = agent.conversation[2]["tool_results"][0]["content"]
    second_content = agent.conversation[6]["tool_results"][0]["content"]
    assert "evicted to save context" in first_content, first_content
    assert second_content == body_2, (
        "most recent skill result was incorrectly evicted"
    )
    print("✓ most recent flagged result preserved (load-bearing)")


def _check_idempotent() -> None:
    """A second walk over already-stubbed results is a no-op."""
    agent = _make_agent(evict_skill=True)
    body = "body content " * 50
    agent.conversation = [
        _user("x"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "read_skill", "args": {"name": "foo"}}
        ]),
        _tool_results([
            {"id": "c1", "name": "read_skill", "content": body}
        ]),
        _assistant(text="consumed"),
    ]

    n1 = agent._apply_eviction()
    assert n1 == 1, f"first walk: expected 1, got {n1}"
    stubbed_content = agent.conversation[2]["tool_results"][0]["content"]

    n2 = agent._apply_eviction()
    assert n2 == 0, f"second walk: expected 0 (idempotent), got {n2}"
    # Content must be unchanged on the no-op walk.
    assert agent.conversation[2]["tool_results"][0]["content"] == stubbed_content
    print("✓ second walk is idempotent (0 new evictions)")


def _check_resume_jsonl_untouched() -> None:
    """JSONL on disk keeps full content; in-memory replay applies eviction."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-evict-resume-"))
    session = Session(session_id="resume", root=tmp)

    body = "Skill body for resume test " * 80  # ~2.2KB
    entries = [
        _user("hello"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "read_skill", "args": {"name": "foo"}}
        ]),
        _tool_results([
            {"id": "c1", "name": "read_skill", "content": body}
        ]),
        _assistant(text="consumed it"),
        _user("more work"),
        _assistant(text="done"),
    ]
    session.append_history(entries)

    # 1. The JSONL on disk contains the full body. Round-trip invariant.
    raw = session.conversation_path.read_text()
    assert body in raw, "full skill body missing from JSONL on disk"
    loaded_back = session.load_history()
    assert loaded_back == entries, (
        "load_history did not round-trip its input — invariant broken"
    )

    # 2. A fresh Agent loads the history and applies the eviction
    # pass. The in-memory conversation gets the stub; the JSONL on
    # disk does NOT change.
    agent = _make_agent(evict_skill=True)
    agent.session = session
    agent.conversation = session.load_history()
    n = agent._apply_eviction()
    assert n == 1, f"expected 1 eviction on resume, got {n}"
    in_mem = agent.conversation[2]["tool_results"][0]["content"]
    assert "evicted to save context" in in_mem, in_mem

    raw_after = session.conversation_path.read_text()
    assert raw_after == raw, "JSONL on disk was mutated by resume eviction"
    assert body in raw_after, "full skill body must still live on disk"

    # Defense-in-depth: each line on disk should still parse and
    # round-trip identically.
    on_disk = [json.loads(line) for line in raw_after.splitlines() if line.strip()]
    assert on_disk == entries, "on-disk JSONL no longer matches what was written"

    print(f"✓ resume: in-memory stubbed, JSONL unchanged ({len(raw)} bytes)")


def _check_multiple_skills_in_sequence() -> None:
    """Each consumed skill gets evicted; only the latest survives."""
    agent = _make_agent(evict_skill=True)
    body_1 = "first body " * 40
    body_2 = "second body " * 40
    body_3 = "third body " * 40

    agent.conversation = [
        _user("multi"),
        _assistant(text="", tool_calls=[
            {"id": "c1", "name": "read_skill", "args": {"name": "a"}}
        ]),
        _tool_results([
            {"id": "c1", "name": "read_skill", "content": body_1}
        ]),
        _assistant(text="got one"),
        _user("ok"),
        _assistant(text="", tool_calls=[
            {"id": "c2", "name": "read_skill", "args": {"name": "b"}}
        ]),
        _tool_results([
            {"id": "c2", "name": "read_skill", "content": body_2}
        ]),
        _assistant(text="got two"),
        _user("more"),
        _assistant(text="", tool_calls=[
            {"id": "c3", "name": "read_skill", "args": {"name": "c"}}
        ]),
        _tool_results([
            {"id": "c3", "name": "read_skill", "content": body_3}
        ]),
        # No assistant-with-output after the third result; it
        # remains load-bearing.
    ]

    n = agent._apply_eviction()
    assert n == 2, f"expected 2 evictions (body_1 and body_2), got {n}"
    first = agent.conversation[2]["tool_results"][0]["content"]
    second = agent.conversation[6]["tool_results"][0]["content"]
    third = agent.conversation[10]["tool_results"][0]["content"]
    assert "evicted to save context" in first, first
    assert "evicted to save context" in second, second
    assert third == body_3, "most recent skill result wrongly evicted"
    print("✓ multiple skills: all consumed bodies evicted, latest preserved")


def main() -> None:
    _check_default_no_eviction()
    _check_evict_after_next_assistant_turn()
    _check_most_recent_preserved()
    _check_idempotent()
    _check_resume_jsonl_untouched()
    _check_multiple_skills_in_sequence()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
