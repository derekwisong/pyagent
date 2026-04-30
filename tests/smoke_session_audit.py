"""Smoke for `pyagent-sessions audit` (issue #14, Part B).

Locks four behaviors:
  1. audit_session aggregates four-key usage correctly across mixed
     pre-#15 (two-key) and post-#15 (four-key) assistant turns.
  2. cost_is_lower_bound is True iff any assistant turn lacks cache
     fields.
  3. The offload-stub regex matches Agent._format_offload_ref output.
  4. inline_bloat ranks largest non-offloaded tool result first.

No subprocess, no LLM. Run with:

    .venv/bin/python -m tests.smoke_session_audit
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pyagent.agent import Agent
from pyagent.sessions_audit import _OFFLOAD_RE, audit_session
from pyagent.sessions_audit_render import (
    ALL_SECTIONS,
    render_json,
    render_text,
)


def _check_offload_regex_matches_format_offload_ref() -> None:
    """The regex must capture path + size from the real format string.

    If `Agent._format_offload_ref` ever changes its prefix, this catches
    it before the audit silently miscounts offloaded vs inline results.
    """
    fake_path = Path(".pyagent/sessions/foo/attachments/fetch_url-ab12cd34.txt")
    stub = Agent._format_offload_ref(fake_path, 12345, "preview-here")
    m = _OFFLOAD_RE.match(stub)
    assert m is not None, f"regex failed to match: {stub[:100]!r}"
    assert m.group(1) == str(fake_path), m.group(1)
    assert m.group(2) == "12345", m.group(2)
    print(f"✓ _OFFLOAD_RE captures path={m.group(1)!r} size={m.group(2)!r}")


def _write_synthetic_session(tmp: Path) -> Path:
    """Mint a session dir with a transcript containing both pre-#15 and
    post-#15 assistant turns, plus inline + offloaded tool results, and
    one orphan attachment on disk."""
    session_dir = tmp / "synthetic-session"
    (session_dir / "attachments").mkdir(parents=True)

    # On-disk attachments: one referenced, one orphan.
    referenced_name = "fetch_url-deadbeef.txt"
    orphan_name = "read_file-cafebabe.txt"
    (session_dir / "attachments" / referenced_name).write_text(
        "x" * 5000
    )
    (session_dir / "attachments" / orphan_name).write_text("y" * 200)

    # Build the offload stub with the real Agent helper so the regex
    # roundtrips. Path matches the on-disk attachment filename so the
    # ref count rolls up correctly.
    ref_path = session_dir / "attachments" / referenced_name
    offload_stub = Agent._format_offload_ref(ref_path, 5000, "preview")

    # Three assistant turns: one pre-#15 (no cache fields), two post-#15.
    entries = [
        {"role": "user", "content": "first prompt"},
        {
            "role": "assistant",
            "text": "ok",
            "tool_calls": [
                {"id": "t1", "name": "fetch_url", "args": {"url": "x"}}
            ],
            "usage": {"input": 100, "output": 50},  # pre-#15
        },
        {
            "role": "user",
            "tool_results": [
                {"id": "t1", "name": "fetch_url", "content": offload_stub},
                {
                    "id": "t1b",
                    "name": "read_file",
                    "content": "small inline result\nline2",
                },
            ],
        },
        {"role": "user", "content": "second prompt"},
        {
            "role": "assistant",
            "text": "ok",
            "tool_calls": [
                {"id": "t2", "name": "execute", "args": {"command": "ls"}}
            ],
            "usage": {
                "input": 200,
                "output": 80,
                "cache_creation": 1000,
                "cache_read": 5000,
            },
        },
        {
            "role": "user",
            "tool_results": [
                # The biggest inline result — must rank first in bloat.
                {
                    "id": "t2",
                    "name": "execute",
                    "content": "huge\n" * 2000,  # 10000 chars
                },
            ],
        },
        {
            "role": "assistant",
            "text": "done",
            "tool_calls": [],
            "usage": {
                "input": 50,
                "output": 25,
                "cache_creation": 0,
                "cache_read": 100,
            },
        },
    ]

    with (session_dir / "conversation.jsonl").open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return session_dir


def _check_audit_synthetic_session() -> None:
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))
        report = audit_session(
            session_dir, model="anthropic/claude-sonnet-4-6"
        )

        assert report.session_id == "synthetic-session", report.session_id
        assert report.turn_count == 3, report.turn_count
        # totals: input 100+200+50=350, output 50+80+25=155
        # cache_creation 0+1000+0=1000, cache_read 0+5000+100=5100
        assert report.total_tokens == {
            "input": 350,
            "output": 155,
            "cache_creation": 1000,
            "cache_read": 5100,
        }, report.total_tokens
        # Pre-#15 turn missing cache fields → lower-bound flag set.
        assert report.cost_is_lower_bound is True, report.cost_is_lower_bound
        # Cost should be a positive float (Sonnet is in the table).
        assert report.total_cost_usd is not None and report.total_cost_usd > 0
        print(
            f"✓ synthetic audit: turn_count={report.turn_count} "
            f"cost={report.total_cost_usd:.4f} "
            f"lower_bound={report.cost_is_lower_bound}"
        )

        # Attachments: 2 files, 1 referenced, 1 orphan.
        assert len(report.attachments) == 2, report.attachments
        by_name = {a.filename: a for a in report.attachments}
        assert by_name["fetch_url-deadbeef.txt"].ref_count == 1
        assert by_name["read_file-cafebabe.txt"].ref_count == 0
        assert report.orphan_attachments == ["read_file-cafebabe.txt"]
        print(
            f"✓ attachments: {len(report.attachments)} files, "
            f"{len(report.orphan_attachments)} orphan(s)"
        )

        # Inline bloat: the 10000-char execute result must rank first;
        # the small "small inline result" must also be present.
        assert report.inline_bloat, report.inline_bloat
        assert report.inline_bloat[0].tool_name == "execute"
        assert report.inline_bloat[0].char_count == 10000
        names = [b.tool_name for b in report.inline_bloat]
        assert "read_file" in names, names
        # Sorted descending: top row > any subsequent row.
        for i in range(len(report.inline_bloat) - 1):
            assert (
                report.inline_bloat[i].char_count
                >= report.inline_bloat[i + 1].char_count
            )
        print(
            f"✓ inline_bloat ranked: top={report.inline_bloat[0].tool_name} "
            f"({report.inline_bloat[0].char_count} chars)"
        )


def _check_render_text_smoke() -> None:
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))
        report = audit_session(session_dir, model="anthropic/claude-sonnet-4-6")
        text = render_text(report, sections=ALL_SECTIONS, top=20, quiet=False)
        assert "session: synthetic-session" in text, text
        assert "PER-TURN BREAKDOWN" in text, text
        assert "ATTACHMENTS" in text, text
        assert "INLINE BLOAT" in text, text
        assert "LOWER BOUND" in text, "lower-bound warning not rendered"
        # quiet=True drops the warning.
        text_quiet = render_text(
            report, sections=ALL_SECTIONS, top=20, quiet=True
        )
        assert "LOWER BOUND" not in text_quiet
        print("✓ render_text emits all four sections + warning gate")


def _check_render_json_smoke() -> None:
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))
        report = audit_session(session_dir, model="anthropic/claude-sonnet-4-6")
        out = render_json(report)
        parsed = json.loads(out)
        assert parsed["session_id"] == "synthetic-session"
        assert parsed["turn_count"] == 3
        assert parsed["cost_is_lower_bound"] is True
        assert isinstance(parsed["per_turn"], list)
        assert isinstance(parsed["attachments"], list)
        print("✓ render_json round-trips")


def _check_section_filtering() -> None:
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))
        report = audit_session(session_dir, model="anthropic/claude-sonnet-4-6")
        # cost-only: header + cost section, no others.
        text = render_text(report, sections={"cost"}, top=20, quiet=True)
        assert "session:" in text  # header always shown
        assert "tokens:" in text and "cost:" in text  # cost section
        assert "PER-TURN BREAKDOWN" not in text, text
        assert "ATTACHMENTS" not in text, text
        assert "INLINE BLOAT" not in text, text

        # bloat-only: header + bloat, but cost section absent.
        text_b = render_text(report, sections={"bloat"}, top=20, quiet=True)
        assert "INLINE BLOAT" in text_b
        assert "PER-TURN BREAKDOWN" not in text_b
        # Cost line lives in the "cost" section now — narrowing past
        # "cost" must drop the tokens / cost lines too. ALL_SECTIONS
        # is honest about what each flag controls.
        assert "tokens:" not in text_b, text_b
        assert "cost:" not in text_b, text_b
        print("✓ section filtering narrows output correctly")


def _check_displayed_total_gates_to_anthropic() -> None:
    """The displayed token total must NOT double-count cached tokens on
    OpenAI / Gemini, where `prompt_tokens` / `prompt_token_count` already
    includes the cached count. Anthropic's `input_tokens` excludes cache,
    so the four-way bundle is correct there. This regression guard locks
    both directions."""
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))

        # Anthropic: bundle all four. totals were:
        #   input 350, output 155, cache_creation 1000, cache_read 5100
        #   sum = 6605 → "6.6K total" in the rendered header
        anth = audit_session(session_dir, model="anthropic/claude-sonnet-4-6")
        text_a = render_text(
            anth, sections={"cost"}, top=20, quiet=True
        )
        assert "6.6K total" in text_a, text_a
        assert "input 350" in text_a and "cache_read 5.1K" in text_a, text_a

        # OpenAI: only input + output. 350 + 155 = 505 → "505 total".
        # Crucially, NOT 6605 / 6.6K (that would mean we double-counted).
        oai = audit_session(session_dir, model="openai/gpt-4o")
        text_o = render_text(
            oai, sections={"cost"}, top=20, quiet=True
        )
        assert "505 total" in text_o, text_o
        assert "6.6K total" not in text_o, (
            f"OpenAI rendered total double-counted cached tokens: {text_o!r}"
        )
        # Gemini: same gate.
        gem = audit_session(session_dir, model="gemini/gemini-2.5-flash")
        text_g = render_text(
            gem, sections={"cost"}, top=20, quiet=True
        )
        assert "505 total" in text_g, text_g
        assert "6.6K total" not in text_g, text_g
        print(
            f"✓ displayed total gates to Anthropic "
            f"(anth=6.6K, openai=505, gemini=505)"
        )


def _check_lower_bound_warning_is_specific() -> None:
    """The warning must name 'X of Y' so the user can judge how
    incomplete the cost number is, not a generic 'at least one'."""
    with tempfile.TemporaryDirectory() as td:
        session_dir = _write_synthetic_session(Path(td))
        report = audit_session(session_dir, model="anthropic/claude-sonnet-4-6")
        # Synthetic session has 1 pre-#15 turn out of 3 total.
        assert report.pre_15_turns == 1, report.pre_15_turns
        text = render_text(
            report, sections=ALL_SECTIONS, top=20, quiet=False
        )
        assert "1 of 3" in text, (
            f"warning should name X of Y, got: {text!r}"
        )
        print("✓ lower-bound warning names X of Y assistant turns")


def main() -> None:
    _check_offload_regex_matches_format_offload_ref()
    _check_audit_synthetic_session()
    _check_render_text_smoke()
    _check_render_json_smoke()
    _check_section_filtering()
    _check_displayed_total_gates_to_anthropic()
    _check_lower_bound_warning_is_specific()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
