"""Smoke for tool-call arg scrubbing.

Large string args in tool_call dicts get replaced with a short
marker after the tool runs, so a write_file with 50KB of content
doesn't ride along on every subsequent LLM call. The original
content survives in two places:
  - The tool already executed against it (so disk has the bytes)
  - The tool result describes the outcome (path, byte count)

The conversation entry is mutated in place, which means the saved
session also has the scrubbed version — bytes deduplicated against
on-disk state.

Run with:

    .venv/bin/python -m tests.smoke_arg_scrubbing
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pyagent.agent import Agent
from pyagent.llms.pyagent import EchoClient


def test_large_arg_scrubbed_after_call() -> None:
    """A tool call with a >4KB string arg has it elided after the
    tool runs. Subsequent turns see the marker, not the bytes."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-scrub-"))
    os.chdir(tmp)
    try:
        agent = Agent(client=EchoClient())

        captured: dict[str, str] = {}

        def big_writer(path: str, content: str) -> str:
            """Write content to a file (test stub)."""
            target = Path(path)
            target.write_text(content)
            captured["seen_content_len"] = str(len(content))
            return f"Wrote {len(content)} bytes to {target}"

        agent.add_tool("write_file", big_writer)

        big_content = "x" * 10_000
        # Build a tool_call dict the way the LLM client would produce it.
        call = {
            "id": "call-1",
            "name": "write_file",
            "args": {"path": str(tmp / "out.txt"), "content": big_content},
        }
        # Simulate the agent loop appending the assistant turn before
        # tool dispatch, so `call["args"]` is the same object that
        # would live in conversation history.
        agent.conversation.append(
            {"role": "assistant", "content": "", "tool_calls": [call]}
        )

        result = agent._route_tool(call)

        # The tool ran with the full content (proves we scrubbed
        # AFTER, not before).
        assert captured["seen_content_len"] == str(len(big_content)), (
            f"tool saw wrong content length: {captured!r}"
        )
        # Tool result reports what happened.
        assert "Wrote 10000 bytes" in result, result

        # The path arg (small) is preserved verbatim.
        assert call["args"]["path"] == str(tmp / "out.txt")
        # The content arg got elided.
        scrubbed = call["args"]["content"]
        assert scrubbed != big_content, "content should be elided"
        assert "elided" in scrubbed, scrubbed
        assert "10000 chars" in scrubbed, scrubbed
        # And the live conversation entry sees the elided form.
        live_call = agent.conversation[-1]["tool_calls"][0]
        assert live_call is call  # same object, mutated in place
        assert "elided" in live_call["args"]["content"]

        print(
            f"✓ large arg scrubbed: {len(big_content)} chars → "
            f"{len(scrubbed)} chars marker"
        )
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_small_args_preserved() -> None:
    """Args under the threshold are left alone — the agent still
    gets to see what it called the tool with on subsequent turns."""
    agent = Agent(client=EchoClient())

    def echo_tool(message: str) -> str:
        """Echo a small string."""
        return f"got: {message}"

    agent.add_tool("echo_tool", echo_tool)

    call = {
        "id": "call-2",
        "name": "echo_tool",
        "args": {"message": "short and sweet"},
    }
    agent.conversation.append(
        {"role": "assistant", "content": "", "tool_calls": [call]}
    )

    agent._route_tool(call)

    assert call["args"]["message"] == "short and sweet"
    print("✓ small args preserved unchanged")


def test_threshold_boundary() -> None:
    """At exactly the threshold, the arg is preserved. Above it, scrubbed."""
    agent = Agent(client=EchoClient())

    def noop(payload: str) -> str:
        """Test helper."""
        return "ok"

    agent.add_tool("noop", noop)

    threshold = Agent.TOOL_ARG_ELIDE_THRESHOLD

    # Exactly at threshold: preserved.
    at_threshold = "y" * threshold
    call_at = {
        "id": "a",
        "name": "noop",
        "args": {"payload": at_threshold},
    }
    agent.conversation.append(
        {"role": "assistant", "content": "", "tool_calls": [call_at]}
    )
    agent._route_tool(call_at)
    assert call_at["args"]["payload"] == at_threshold

    # One char over: scrubbed.
    over = "z" * (threshold + 1)
    call_over = {
        "id": "b",
        "name": "noop",
        "args": {"payload": over},
    }
    agent.conversation.append(
        {"role": "assistant", "content": "", "tool_calls": [call_over]}
    )
    agent._route_tool(call_over)
    assert "elided" in call_over["args"]["payload"]
    print(f"✓ threshold boundary: ≤{threshold} preserved, >{threshold} scrubbed")


def main() -> None:
    test_large_arg_scrubbed_after_call()
    test_small_args_preserved()
    test_threshold_boundary()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
