"""Smoke for the read_file soft-threshold ceiling (issue #9).

Locks four behaviors:
  1. Small read_file output (< attachment_threshold) returns inline.
  2. Large read_file output (between threshold and hard ceiling) is
     forced to offload, returns the stub string.
  3. Tools registered with auto_offload=False that are NOT in
     SOFT_THRESHOLD_FORCED_TOOLS (e.g. read_skill) still bypass the
     soft threshold.
  4. Hard ceiling still fires for tools outside the forced set when
     output exceeds HARD_OFFLOAD_CEILING.

Constructed via Agent._render_tool_result(name, text) directly with a
real Session in a tempdir — no Attachment(...) construction site is
introduced (smoke_session_replay enforces that invariant).

No subprocess, no network. Run with:
    .venv/bin/python -m tests.smoke_read_file_ceiling
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent.agent import Agent
from pyagent.session import Session


def _make_agent(tmp: Path) -> Agent:
    session = Session(session_id="ceiling", root=tmp)
    session._ensure_dirs()
    agent = Agent(client=None, session=session)  # client unused here
    # Mirror agent_proc registration: read_file and read_skill bypass
    # auto_offload, every other tool defaults to True.
    agent.add_tool("read_file", lambda: None, auto_offload=False)
    agent.add_tool("read_skill", lambda: None, auto_offload=False)
    agent.add_tool("some_normal_tool", lambda: None, auto_offload=True)
    return agent


def _check_small_read_file_inline() -> None:
    """A 200-char read_file result returns the original text inline."""
    with tempfile.TemporaryDirectory(prefix="pyagent-smoke-ceiling-") as t:
        agent = _make_agent(Path(t))
        text = "x" * 200
        rendered = agent._render_tool_result("read_file", text)
        assert rendered == text, (
            f"small read_file should return inline; got {rendered[:120]!r}"
        )
    print("✓ small read_file output returns inline")


def _check_large_read_file_offloads() -> None:
    """A 17_500-char read_file (> 8000 soft threshold, < 64000 hard ceiling)
    is forced to offload via the SOFT_THRESHOLD_FORCED_TOOLS path."""
    with tempfile.TemporaryDirectory(prefix="pyagent-smoke-ceiling-") as t:
        agent = _make_agent(Path(t))
        threshold = agent.session.attachment_threshold
        ceiling = agent.HARD_OFFLOAD_CEILING
        size = 17_500
        assert threshold < size < ceiling, (
            f"sanity: {threshold} < {size} < {ceiling}"
        )
        text = "y" * size
        rendered = agent._render_tool_result("read_file", text)
        assert "[output saved to" in rendered, rendered
        assert text not in rendered, (
            "raw payload leaked through soft-threshold offload"
        )
    print(f"✓ {size}-char read_file forced offload via soft threshold")


def _check_read_skill_unaffected() -> None:
    """read_skill is auto_offload=False but NOT in
    SOFT_THRESHOLD_FORCED_TOOLS, so a 17_500-char result still returns
    inline. This is the regression guard for #10."""
    with tempfile.TemporaryDirectory(prefix="pyagent-smoke-ceiling-") as t:
        agent = _make_agent(Path(t))
        assert "read_skill" not in agent.SOFT_THRESHOLD_FORCED_TOOLS
        text = "z" * 17_500
        rendered = agent._render_tool_result("read_skill", text)
        assert rendered == text, (
            "read_skill should bypass soft threshold; got offload stub"
        )
    print("✓ read_skill bypasses soft threshold (regression guard for #10)")


def _check_hard_ceiling_still_fires() -> None:
    """Outputs over HARD_OFFLOAD_CEILING are offloaded regardless of
    auto_offload, even for tools outside the forced set."""
    with tempfile.TemporaryDirectory(prefix="pyagent-smoke-ceiling-") as t:
        agent = _make_agent(Path(t))
        ceiling = agent.HARD_OFFLOAD_CEILING
        text = "q" * (ceiling + 5_000)
        rendered = agent._render_tool_result("read_skill", text)
        assert "[output saved to" in rendered, rendered
        assert text not in rendered
    print("✓ hard ceiling still offloads oversize read_skill output")


def main() -> None:
    _check_small_read_file_inline()
    _check_large_read_file_offloads()
    _check_read_skill_unaffected()
    _check_hard_ceiling_still_fires()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
