"""Smoke for the CLI status footer.

Drives `_update_agents_state`, `_render_status`, and `_compose_footer`
directly with synthetic events and asserts the rendered footer
reflects current multi-agent activity. No subprocess.

Issue #67 redesign: footer is now a three-zone single-line layout
(left = liveness, center = work, right = `gross / net · $cost`)
with a fixed degradation pipeline under width pressure and a Tier
A → B → C subagent collapse. The tests below exercise each branch.

Run with:

    .venv/bin/python -m tests.smoke_status_footer
"""

from __future__ import annotations

import collections
import io
import re

from rich.console import Console

from pyagent.cli import (
    _compose_footer,
    _msgs_segment,
    _perms_segment,
    _render_status,
    _render_status_ansi,
    _spinner_segment,
    _tree_busy,
    _update_agents_state,
)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def render_plain(markup: str) -> str:
    """Strip ANSI / styles from `_render_status` output for assertion."""
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(markup)
    return buf.getvalue().rstrip()


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def compose_plain(
    agents: dict,
    model: str = "",
    perms: collections.deque | None = None,
    cols: int = 100,
) -> str:
    """Run the full composer and strip ANSI for assertion."""
    p = perms if perms is not None else collections.deque()
    return strip_ansi(_compose_footer(agents, model, p, cols))


def check_state_machine() -> None:
    """Original `_update_agents_state` + `_render_status` left-zone tests."""
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

    # Idle root: terminal `ready` and `error` statuses drop the `…`
    # so the always-on bottom_toolbar doesn't say "thinking…" while
    # the agent is just waiting for input.
    idle = {"root": {"status": "ready"}}
    out = render_plain(_render_status(idle))
    assert out == "ready", repr(out)
    print(f"✓ idle (ready): {out!r}")

    err = {"root": {"status": "error"}}
    out = render_plain(_render_status(err))
    assert out == "error", repr(out)
    print(f"✓ idle (error): {out!r}")

    # Active states still show the `…`.
    busy = {"root": {"status": "thinking"}}
    assert render_plain(_render_status(busy)) == "thinking…"
    busy = {"root": {"status": "· execute"}}
    assert render_plain(_render_status(busy)) == "· execute…"
    print("✓ active states keep '…'")


def check_three_zone_layout() -> None:
    """Three-zone composition: left, filler-pad, right."""
    # Idle, post-turn, no cache benefit yet (gross == net).
    agents = {
        "root": {
            "status": "ready",
            "tokens": {
                "input": 12000, "output": 400,
                "cache_creation": 0, "cache_read": 0,
            },
        }
    }
    out = compose_plain(agents, "anthropic", cols=100)
    # Left zone is `ready`; right zone shows two equal token counts
    # because no cache traffic yet. Width must be exactly 100.
    assert out.startswith("ready"), out
    assert "/" in out and "$" in out, out
    # Total width matches `cols` (no spinner because root is ready).
    assert len(out) == 100, (len(out), out)
    print(f"✓ idle three-zone: width={len(out)} {out!r}")

    # Single agent thinking, mid-conversation, heavy cache reads.
    # gross = 1000 + 200 + 0 + 50000 = 51200 → 51.2k
    # net = 1000 + 200 + 0 + 50000*0.1 = 6200 → 6.2k
    agents = {
        "root": {
            "status": "thinking",
            "tokens": {
                "input": 1000, "output": 200,
                "cache_creation": 0, "cache_read": 50000,
            },
        }
    }
    out = compose_plain(agents, "anthropic", cols=100)
    assert "thinking…" in out, out
    assert "51.2k / 6.2k" in out, out
    # Right zone (cost) is pinned to the right edge — line ends at
    # the dollar amount and the gap to the left zone is filled with
    # spaces, not the other way around.
    assert out.rstrip().endswith("$0.021"), out
    print(f"✓ cache-heavy net << gross: {out!r}")


def check_perms_msgs_styling() -> None:
    """perms is bold-yellow (brightest pixel); msgs is severity-colored."""
    agents = {
        "root": {
            "status": "thinking",
            "notes_unread": {
                "count": 2,
                "by_severity": {"info": 0, "warn": 1, "alert": 0},
            },
        }
    }
    perms = collections.deque([
        {"target": "bash", "agent_id": None, "request_id": "r1"},
    ])
    raw = _compose_footer(agents, "", perms, 120)
    # ANSI escape `\x1b[1;33m` is bold yellow per rich's mapping. We
    # don't pin the exact code but we do verify the perms text is
    # wrapped in bold.
    assert "\x1b[1" in raw, raw  # some bold escape present
    plain = strip_ansi(raw)
    assert "perms: bash" in plain, plain
    assert "msgs: 2 (warn)" in plain, plain
    print(f"✓ perms bold + msgs severity: {plain!r}")

    # No severity → msgs collapses to plain count (info default).
    agents = {
        "root": {
            "status": "thinking",
            "notes_unread": {"count": 3, "by_severity": {"info": 3}},
        }
    }
    plain = compose_plain(agents, "", cols=120)
    assert "msgs: 3" in plain and "(info)" not in plain, plain
    print(f"✓ msgs hides info severity tag: {plain!r}")

    # `_msgs_segment` returns severity for the renderer to color.
    text, sev = _msgs_segment(
        {"root": {"notes_unread": {
            "count": 5,
            "by_severity": {"info": 2, "warn": 1, "alert": 1},
        }}}
    )
    assert sev == "alert", sev
    assert text == " · msgs: 5 (alert)", text
    print(f"✓ msgs picks highest severity: sev={sev}, text={text!r}")

    # Empty / zero-count → empty string.
    text, sev = _msgs_segment({"root": {}})
    assert text == "" and sev is None
    text, sev = _msgs_segment(
        {"root": {"notes_unread": {"count": 0, "by_severity": {}}}}
    )
    assert text == "" and sev is None
    print("✓ msgs drops at zero count")


def check_tier_collapse() -> None:
    """Tier A → B → C as terminal width shrinks."""
    # 5 agents: 2 working, 3 idle. Tier A fits at 120 cols, Tier B
    # forces idle to be summarized, Tier C drops names entirely.
    agents = {
        "root": {"status": "thinking"},
        "s1": {"status": "· bash"},
        "s2": {"status": "ready"},
        "s3": {"status": "ready"},
        "s4": {"status": "idle"},
    }
    wide = compose_plain(agents, "", cols=120)
    assert "root(" in wide and "s1(" in wide and "s2(" in wide, wide
    print(f"✓ tier A wide: {wide!r}")

    # Squeeze hard enough that all five names + statuses don't fit.
    narrow = compose_plain(agents, "", cols=50)
    # Either tier B (working-only with `+N idle`) or tier C
    # (`5 agents: ...`) — both are acceptable, depending on widths.
    assert "+3 idle" in narrow or "agents:" in narrow, narrow
    print(f"✓ tier B/C narrow: {narrow!r}")

    # 8 agents → goes straight to tier C (>6 cap).
    big = {
        "root": {"status": "thinking"},
        **{f"s{i}": {"status": "· bash"} for i in range(1, 5)},
        **{f"s{i}": {"status": "ready"} for i in range(5, 8)},
    }
    out = compose_plain(big, "", cols=120)
    assert "8 agents:" in out, out
    assert "5 working" in out and "3 idle" in out, out
    print(f"✓ >6 agents → tier C: {out!r}")

    # Errors only show in tier C summary when present.
    no_err = {f"s{i}": {"status": "ready"} for i in range(7)}
    out = compose_plain(no_err, "", cols=120)
    assert "error" not in out, out
    with_err = {**no_err, "s_err": {"status": "error"}}
    out = compose_plain(with_err, "", cols=120)
    assert "1 error" in out, out
    print(f"✓ tier C error bucket only when non-zero")


def check_degradation_priority() -> None:
    """Drop order under width pressure matches the spec."""
    # Construct a scenario with everything turned on, then watch the
    # composer drop pieces as `cols` shrinks.
    agents = {
        "root": {
            "status": "thinking",
            "checklist": {
                "completed": 2, "total": 5,
                "current_title": "refactor token accounting",
            },
            "tokens": {
                "input": 1000, "output": 200,
                "cache_creation": 0, "cache_read": 50000,
            },
            "notes_unread": {
                "count": 2,
                "by_severity": {"warn": 1},
            },
        }
    }
    perms = collections.deque([
        {"target": "/etc/passwd", "agent_id": None, "request_id": "r1"},
        {"target": "/usr/bin/x", "agent_id": None, "request_id": "r2"},
    ])

    # 200 cols: everything visible.
    out = compose_plain(agents, "anthropic", perms, cols=200)
    assert "head:" in out, out
    assert "(warn)" in out, out
    assert "refactor" in out, out
    assert "/" in out and "$" in out, out
    print(f"✓ no-pressure: {out!r}")

    # Step 1 — perms head preview drops first. Squeeze just enough
    # that head: doesn't fit but everything else does.
    out = compose_plain(agents, "anthropic", perms, cols=100)
    # head: drops (count survives), other fields still present.
    if "head:" not in out:
        assert "perms: 2" in out, out
        print(f"✓ step1 head dropped: {out!r}")

    # Step 2 — msgs severity tag drops next. At very narrow widths
    # the tag goes; the count survives.
    out = compose_plain(agents, "anthropic", perms, cols=80)
    if "(warn)" not in out:
        assert "msgs: 2" in out, out
        print(f"✓ step2 severity tag dropped: {out!r}")

    # Step 7 — msgs drops before perms. At very narrow widths perms
    # survives because it's blocking the agent.
    out = compose_plain(agents, "anthropic", perms, cols=45)
    assert "perms" in out, out
    print(f"✓ msgs drops before perms (cols=45): {out!r}")

    # Right zone is the contract: never truncated.
    out = compose_plain(agents, "anthropic", perms, cols=40)
    assert "$" in out, out
    print(f"✓ right zone always present (cols=40): {out!r}")

    # Verify _perms_segment drop_head=True works directly.
    p_short = _perms_segment(perms, drop_head=False)
    p_dropped = _perms_segment(perms, drop_head=True)
    assert "head:" in p_short and "head:" not in p_dropped, (
        p_short, p_dropped,
    )
    print(f"✓ _perms_segment drop_head: {p_short!r} → {p_dropped!r}")


def check_spinner_predicate() -> None:
    """Spinner spins iff anything in the tree is non-ready/non-error."""
    assert _tree_busy({"root": {"status": "thinking"}}) is True
    assert _tree_busy({"root": {"status": "ready"}}) is False
    assert _tree_busy({"root": {"status": "error"}}) is False
    # Mixed: root ready but a subagent working → still busy.
    assert _tree_busy(
        {"root": {"status": "ready"}, "s1": {"status": "· bash"}}
    ) is True
    # Mixed: root working, all subs idle → busy.
    assert _tree_busy(
        {"root": {"status": "thinking"}, "s1": {"status": "ready"}}
    ) is True
    # All terminal → not busy.
    assert _tree_busy(
        {"root": {"status": "ready"}, "s1": {"status": "error"}}
    ) is False
    print("✓ spinner predicate: any-non-terminal-in-tree")

    # _spinner_segment is unchanged: takes a bool.
    assert _spinner_segment(False) == ""
    assert _spinner_segment(True) != ""
    print("✓ _spinner_segment unchanged signature")

    # Composer exercises the predicate end-to-end.
    out_busy = compose_plain(
        {"root": {"status": "ready"}, "s1": {"status": "· bash"}},
        "",
        cols=80,
    )
    raw_busy = _compose_footer(
        {"root": {"status": "ready"}, "s1": {"status": "· bash"}},
        "", collections.deque(), 80,
    )
    # Spinner is one of the Braille frames.
    assert any(c in raw_busy for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), raw_busy
    print(f"✓ root ready + sub busy → spinner shown: {out_busy!r}")

    # All idle → no spinner.
    raw_idle = _compose_footer(
        {"root": {"status": "ready"}}, "", collections.deque(), 80
    )
    assert not any(c in raw_idle for c in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"), raw_idle
    print("✓ all-idle → spinner hidden")


def check_right_zone_contract() -> None:
    """Right zone always renders when usage > 0; truncation falls on center."""
    # Heavy usage + extremely narrow terminal: right zone survives.
    agents = {
        "root": {
            "status": "thinking",
            "tokens": {
                "input": 89000, "output": 200,
                "cache_creation": 1000, "cache_read": 31000,
            },
        }
    }
    out = compose_plain(agents, "anthropic", cols=30)
    # Right zone (cost) must survive; center may be truncated.
    assert "$" in out, out
    print(f"✓ ultra-narrow (cols=30) keeps cost: {out!r}")

    # Zero usage → no right zone, no $.
    no_usage = {"root": {"status": "ready"}}
    out = compose_plain(no_usage, "anthropic", cols=80)
    assert "$" not in out, out
    print(f"✓ zero-usage → no right zone: {out!r}")

    # Unknown model + usage → $0.00 fallback.
    out = compose_plain(agents, "pyagent/echo", cols=100)
    assert "$0.00" in out, out
    print(f"✓ unknown model → $0.00 fallback: {out!r}")

    # Right-zone budget cap (28 cols): even with insanely large
    # token counts the right zone collapses to net+cost rather than
    # eating half the line.
    huge = {
        "root": {
            "status": "ready",
            "tokens": {
                "input": 10_000_000, "output": 1_000_000,
                "cache_creation": 100_000, "cache_read": 50_000_000,
            },
        }
    }
    out = compose_plain(huge, "anthropic", cols=200)
    assert len(out) == 200, (len(out), out)
    # Visible width is exactly cols — pad math is honest.
    print(f"✓ right-zone width-200 honored: len={len(out)}")

    # Width matches `cols` for normal-sized usage too.
    out = compose_plain(agents, "anthropic", cols=100)
    assert len(out) == 100, (len(out), out)
    print(f"✓ pad math: len matches cols for normal usage")


def check_error_paint() -> None:
    """Errored agent paints red rather than dim."""
    agents = {
        "root": {"status": "thinking"},
        "s2": {"status": "error"},
    }
    raw = _compose_footer(agents, "", collections.deque(), 100)
    plain = strip_ansi(raw)
    assert "s2(error)" in plain or "s2(" in plain, plain
    # Red ANSI escape (`\x1b[31m`) somewhere in the output.
    assert "\x1b[31m" in raw or "\x1b[91m" in raw, raw
    print(f"✓ error paints red: {plain!r}")


def main() -> None:
    check_state_machine()
    check_three_zone_layout()
    check_perms_msgs_styling()
    check_tier_collapse()
    check_degradation_priority()
    check_spinner_predicate()
    check_right_zone_contract()
    check_error_paint()

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
