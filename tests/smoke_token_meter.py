"""Smoke for the token / cost meter.

Exercises (in-process):
  1. Agent.token_usage accumulates across multiple LLM calls.
  2. on_usage callback fires per call with the usage dict.
  3. _update_agents_state handles `usage` events and accumulates
     per-agent.
  4. _render_status renders the token+cost suffix with sensible
     formatting for known and unknown models.
  5. Cache-token aggregation: cache_creation / cache_read flow from
     stub usage dicts through Agent.token_usage and `usage` events,
     and the Anthropic-cache cost multipliers feed _estimate_cost_usd.
  6. Backward compatibility: events / state predating the four-key
     usage schema do not KeyError on cache fields.

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
        system_volatile: str | None = None,
    ) -> dict[str, Any]:
        u = self.usages[min(self.calls, len(self.usages) - 1)]
        self.calls += 1
        return {
            "role": "assistant",
            "text": f"reply-{self.calls}",
            "tool_calls": [],
            "usage": {
                "input": u[0],
                "output": u[1],
                "cache_creation": 0,
                "cache_read": 0,
            },
        }


class StubClientWithCacheUsage:
    """Stub LLM that returns a fixed four-key usage dict including
    Anthropic-shape cache_creation / cache_read counts."""

    model = "stub-cache"

    def __init__(
        self,
        input_t: int = 100,
        output_t: int = 50,
        cache_creation: int = 1000,
        cache_read: int = 500,
    ) -> None:
        self.input_t = input_t
        self.output_t = output_t
        self.cache_creation = cache_creation
        self.cache_read = cache_read

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
    ) -> dict[str, Any]:
        return {
            "role": "assistant",
            "text": "cache-reply",
            "tool_calls": [],
            "usage": {
                "input": self.input_t,
                "output": self.output_t,
                "cache_creation": self.cache_creation,
                "cache_read": self.cache_read,
            },
        }


def _render_plain(markup: str) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(markup)
    return buf.getvalue().rstrip()


def _check_cache_token_aggregation() -> None:
    """Cache token fields aggregate end-to-end and feed cost math."""
    # Agent aggregates all four keys.
    agent = Agent(client=StubClientWithCacheUsage())
    agent.run("hi")
    assert agent.token_usage == {
        "input": 100,
        "output": 50,
        "cache_creation": 1000,
        "cache_read": 500,
    }, agent.token_usage
    print(f"✓ Agent.token_usage four-key aggregation: {agent.token_usage}")

    # Cost arithmetic for Anthropic with cache fields.
    # base = 1000*3 + 500*15 = 3000 + 7500 = 10500
    # write = 1000 * 3 * 1.25 = 3750
    # read = 500 * 3 * 0.1 = 150
    # total = 14400 / 1_000_000 = 0.0144
    cost = _estimate_cost_usd(
        "anthropic/claude-sonnet-4-6", 1000, 500, 1000, 500
    )
    assert cost is not None and abs(cost - 0.0144) < 1e-9, cost
    print(f"✓ Anthropic cache-aware cost: {cost}")

    # Non-Anthropic models must ignore cache multipliers.
    # gpt-4o: 1000*2.5 + 500*10 = 2500 + 5000 = 7500 → 0.0075
    cost_oa = _estimate_cost_usd("openai/gpt-4o", 1000, 500, 100, 100)
    assert cost_oa is not None and abs(cost_oa - 0.0075) < 1e-9, cost_oa
    print(f"✓ Non-Anthropic ignores cache multipliers: {cost_oa}")

    # Backward compat: usage event missing cache_creation / cache_read.
    agents: dict[str, dict] = {}
    _update_agents_state(
        agents, {"type": "usage", "input": 10, "output": 5}
    )
    assert agents["root"]["tokens"] == {
        "input": 10,
        "output": 5,
        "cache_creation": 0,
        "cache_read": 0,
    }, agents
    print("✓ usage event missing cache keys → defaults to zero")

    # Backward compat: pre-existing two-key tokens dict (e.g. from a
    # long-running CLI process started before this PR landed) must
    # accept new four-key events without KeyError.
    legacy = {"root": {"status": "idle", "tokens": {"input": 50, "output": 30}}}
    _update_agents_state(
        legacy,
        {
            "type": "usage",
            "input": 1,
            "output": 2,
            "cache_creation": 3,
            "cache_read": 4,
        },
    )
    assert legacy["root"]["tokens"] == {
        "input": 51,
        "output": 32,
        "cache_creation": 3,
        "cache_read": 4,
    }, legacy
    print("✓ legacy two-key tokens dict accepts cache keys")

    # Suffix activity threshold: cache_read alone counts as activity.
    suf = _format_usage_suffix(0, 0, "anthropic/claude-sonnet-4-6", 0, 100)
    assert suf, f"suffix should be non-empty when cache_read > 0: {suf!r}"
    assert "tok" in suf, suf
    print(f"✓ suffix activates on cache-only usage: {suf!r}")

    # On Anthropic, the four token counts are disjoint (input excludes
    # both cache reads and writes), so the displayed total bundles all
    # four. On OpenAI / Gemini, the providers' "input" already includes
    # the cached count — bundling cache_read on top would double-count.
    # The display must gate the bundle the same way _estimate_cost_usd
    # gates the cache pricing multipliers.
    anth = _format_usage_suffix(
        100, 50, "anthropic/claude-sonnet-4-6", 200, 1000
    )
    assert "1.4k tok" in anth, anth  # 100 + 50 + 200 + 1000 = 1350 → 1.4k
    oai = _format_usage_suffix(100, 50, "openai/gpt-4o", 0, 1000)
    # OpenAI's prompt_tokens already includes cache_read; total must
    # not double-count. Expected: input + output = 150 (NOT 1150).
    assert "150 tok" in oai, oai
    assert "1.1k tok" not in oai, (
        f"OpenAI suffix double-counted cache_read into displayed total: {oai!r}"
    )
    gem = _format_usage_suffix(100, 50, "gemini/gemini-2.5-flash", 0, 1000)
    assert "150 tok" in gem, gem
    assert "1.1k tok" not in gem, (
        f"Gemini suffix double-counted cache_read into displayed total: {gem!r}"
    )
    print(f"✓ suffix gates cache bundling to Anthropic (anth={anth!r}, oai={oai!r})")


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
    assert agent.token_usage == {
        "input": 100,
        "output": 50,
        "cache_creation": 0,
        "cache_read": 0,
    }, agent.token_usage
    agent.run("second")
    assert agent.token_usage == {
        "input": 300,
        "output": 130,
        "cache_creation": 0,
        "cache_read": 0,
    }, agent.token_usage
    print(f"✓ Agent.token_usage accumulates: {agent.token_usage}")

    # 5. on_usage fires per call
    captured: list[dict] = []
    agent2 = Agent(client=StubClientWithUsage([(50, 25)]))
    agent2.run("ping", on_usage=lambda u: captured.append(u))
    assert captured == [
        {
            "input": 50,
            "output": 25,
            "cache_creation": 0,
            "cache_read": 0,
        }
    ], captured
    print(f"✓ on_usage callback fires: {captured}")

    # 6. _update_agents_state usage handling
    agents: dict[str, dict] = {"root": {"status": "thinking"}}
    _update_agents_state(
        agents, {"type": "usage", "input": 100, "output": 50}
    )
    assert agents["root"]["tokens"] == {
        "input": 100,
        "output": 50,
        "cache_creation": 0,
        "cache_read": 0,
    }, agents
    _update_agents_state(
        agents, {"type": "usage", "input": 200, "output": 80}
    )
    assert agents["root"]["tokens"] == {
        "input": 300,
        "output": 130,
        "cache_creation": 0,
        "cache_read": 0,
    }, agents
    # subagent usage event creates the slot if missing
    _update_agents_state(
        agents,
        {"type": "usage", "agent_id": "lead-x", "input": 50, "output": 20},
    )
    assert agents["lead-x"]["tokens"] == {
        "input": 50,
        "output": 20,
        "cache_creation": 0,
        "cache_read": 0,
    }, agents
    print("✓ usage events accumulate per-agent")

    # 7. _agents_tokens sums (now four-tuple)
    in_tot, out_tot, cw_tot, cr_tot = _agents_tokens(agents)
    assert in_tot == 350 and out_tot == 150, (in_tot, out_tot)
    assert cw_tot == 0 and cr_tot == 0, (cw_tot, cr_tot)
    print(f"✓ aggregate: {in_tot} in / {out_tot} out / {cw_tot} cw / {cr_tot} cr")

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

    # 9. Cache-aware extensions
    _check_cache_token_aggregation()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
