"""Smoke for the token / cost meter.

Exercises (in-process):
  1. Agent.token_usage accumulates across multiple LLM calls.
  2. on_usage callback fires per call with the usage dict.
  3. _update_agents_state handles `usage` events and accumulates
     per-agent.
  4. _render_status renders the token+cost suffix with sensible
     formatting for known and unknown models.

No subprocess, no real network. Run with:

    .venv/bin/python -m tests.smoke_token_meter
"""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console

from pyagent.agent import Agent
from pyagent.cli import (
    _agents_tokens,
    _estimate_cost_usd,
    _format_usage_suffix,
    _model_name,
    _render_status,
    _update_agents_state,
)


class StubClientWithUsage:
    """Stub LLM that returns a configurable usage block per call.

    Returns no tool calls (so agent.run terminates after one turn)
    and a fixed text. Each `respond` increments turn count and
    returns the usage tuple at that index.
    """

    model = "stub"

    def __init__(self, usages: list[tuple[int, int]]):
        self.usages = usages
        self.calls = 0

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        u = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        return {
            "role": "assistant",
            "text": f"reply-{self.calls}",
            "tool_calls": [],
            "usage": {"input": u[0], "output": u[1]},
        }


def _render_plain(markup: str) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(markup)
    return buf.getvalue().rstrip()


def main() -> None:
    # 1. Model-name resolution
    assert _model_name("anthropic") == "claude-sonnet-4-6", _model_name("anthropic")
    assert _model_name("anthropic/claude-opus-4-7") == "claude-opus-4-7"
    assert _model_name("pyagent/echo") == "echo"
    assert _model_name("openai") == "gpt-4o"
    print("✓ default model resolution")

    # 2. Cost estimation arithmetic
    cost = _estimate_cost_usd("anthropic/claude-sonnet-4-6", 1000, 500)
    assert abs(cost - 0.0105) < 1e-6, cost
    cost = _estimate_cost_usd("pyagent/echo", 1000, 1000)
    assert cost is None, cost  # unknown model
    print("✓ cost estimation")

    # 3. Formatting
    assert _format_usage_suffix(0, 0, "anthropic") == ""
    assert "tok" in _format_usage_suffix(50, 30, "anthropic")
    assert "$" in _format_usage_suffix(50, 30, "anthropic")
    # Unknown model → token-only suffix
    suf = _format_usage_suffix(100, 50, "pyagent/echo")
    assert "tok" in suf and "$" not in suf, suf
    # Big numbers use kilo
    suf = _format_usage_suffix(2500, 1000, "anthropic")
    assert "k tok" in suf, suf
    print("✓ formatting")

    # 4. Agent accumulates token_usage
    client = StubClientWithUsage([(100, 50), (200, 80)])
    agent = Agent(client=client)
    agent.run("first")
    assert agent.token_usage == {"input": 100, "output": 50}, agent.token_usage
    agent.run("second")
    assert agent.token_usage == {"input": 300, "output": 130}, agent.token_usage
    print(f"✓ Agent.token_usage accumulates: {agent.token_usage}")

    # 5. on_usage fires per call
    captured: list[dict] = []
    agent2 = Agent(client=StubClientWithUsage([(50, 25)]))
    agent2.run("ping", on_usage=lambda u: captured.append(u))
    assert captured == [{"input": 50, "output": 25}], captured
    print(f"✓ on_usage callback fires: {captured}")

    # 6. _update_agents_state usage handling
    agents: dict[str, dict] = {"root": {"status": "thinking"}}
    _update_agents_state(
        agents, {"type": "usage", "input": 100, "output": 50}
    )
    assert agents["root"]["tokens"] == {"input": 100, "output": 50}, agents
    _update_agents_state(
        agents, {"type": "usage", "input": 200, "output": 80}
    )
    assert agents["root"]["tokens"] == {"input": 300, "output": 130}, agents
    # subagent usage event creates the slot if missing
    _update_agents_state(
        agents,
        {"type": "usage", "agent_id": "lead-x", "input": 50, "output": 20},
    )
    assert agents["lead-x"]["tokens"] == {"input": 50, "output": 20}, agents
    print(f"✓ usage events accumulate per-agent")

    # 7. _agents_tokens sums
    in_tot, out_tot = _agents_tokens(agents)
    assert in_tot == 350 and out_tot == 150, (in_tot, out_tot)
    print(f"✓ aggregate: {in_tot} in / {out_tot} out")

    # 8. _render_status includes the suffix
    out = _render_plain(_render_status(agents, "anthropic"))
    assert "tok" in out and "$" in out, out
    print(f"✓ render with cost: {out!r}")

    out = _render_plain(_render_status(agents, "pyagent/echo"))
    assert "tok" in out and "$" not in out, out
    print(f"✓ render without cost: {out!r}")

    # Single-agent, zero tokens → no suffix
    minimal = {"root": {"status": "thinking"}}
    out = _render_plain(_render_status(minimal, "anthropic"))
    assert out == "thinking…", out
    print(f"✓ render zero-token: {out!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
