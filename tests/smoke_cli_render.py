"""End-to-end smoke for the CLI's per-event rendering choices that
came out of issue #111.

Concerns:

  1. **Streaming `assistant_text` does not double-render.** When a
     provider streams text deltas and then closes with the full
     `assistant_text`, the closing event must NOT emit the full
     text again as a Markdown block — that produced a visible
     duplicate (the streamed dim line, then the same content as
     a markdown render right below). Now the closing event just
     ends the streamed row with a blank line.
  2. **Non-streaming `assistant_text` still renders as Markdown.**
     Providers without delta callbacks (anthropic/openai/gemini
     in their current shape here) keep the full markdown render.
  3. **`_commit_user_line` walks back over the just-echoed
     prompt and re-renders the user's line as a shaded historical
     row.** Always erases the prompt's divider + arrow rows
     (`\\x1b[2F\\x1b[J`); for non-empty input, additionally writes
     `│ <line>` with bold foreground + dark-grey background that
     extends to the terminal width. Empty input erases only —
     no scrollback artifact for accidental Enters.

Run with:

    .venv/bin/python -m tests.smoke_cli_render
"""

from __future__ import annotations

import io
import os
import sys
from unittest import mock

from pyagent import cli


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _capture_print_calls(monkeypatch_target):
    """Patch `cli.console.print` and return a list that collects
    each call's positional args + kwargs. Lets us assert what was
    rendered without owning a real terminal."""
    calls: list[tuple] = []

    def fake_print(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch_target(cli.console, "print", fake_print)
    return calls


def _check_streaming_assistant_text_walks_back_and_renders_markdown() -> None:
    # Simulate: deltas flowed → `_streaming_active` has "root" with
    # 3 rendered rows recorded → closing assistant_text arrives.
    # Expected: stdout receives a walk-back ANSI sized to the
    # rendered region, then a Markdown render lands in its place.
    cli._streaming_active.clear()
    cli._streaming_active.add("root")
    cli._streaming_text["root"] = "Hello! How can I assist you today?"
    cli._streaming_rendered_rows["root"] = 3

    buf = io.StringIO()
    with mock.patch.object(sys, "stdout", buf), \
         mock.patch.object(cli.console, "print") as fake_print:
        cli._print_event({"type": "assistant_text", "text": "**hi**"})

    out = buf.getvalue()
    _check(
        "stream close walks back exactly the rendered row count",
        "\x1b[3F\x1b[J" in out,
        repr(out),
    )
    _check(
        "stream close drops cumulative streaming state",
        "root" not in cli._streaming_active
        and "root" not in cli._streaming_text
        and "root" not in cli._streaming_rendered_rows,
    )
    rendered_markdown = any(
        any(isinstance(a, cli.Markdown) for a in call.args)
        for call in fake_print.call_args_list
    )
    _check(
        "stream close re-renders the final text as Markdown",
        rendered_markdown,
        f"calls={fake_print.call_args_list}",
    )


def _check_on_text_delta_renders_cumulatively() -> None:
    # Simulate two deltas. Expected: the first writes a walk-back
    # of zero (initial render) + the chunk text. The second walks
    # back over the first render and re-emits the cumulative text.
    cli._streaming_active.clear()
    cli._streaming_text.clear()
    cli._streaming_rendered_rows.clear()

    # Force a wide-enough terminal that "Hello"/"Hello!" each fit
    # on one row → first render = 1 row, walk-back = \x1b[1F.
    with mock.patch.object(
        cli.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
    ), mock.patch.object(cli.console, "print") as fake_print:
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            cli._on_text_delta("Hello", agent_id=None)
        first = buf.getvalue()
        buf2 = io.StringIO()
        with mock.patch.object(sys, "stdout", buf2):
            cli._on_text_delta("!", agent_id=None)
        second = buf2.getvalue()

    _check(
        "first delta writes the chunk without prior walk-back",
        "\x1b[F" not in first and "Hello" in first,
        repr(first),
    )
    _check(
        "first delta writes the dim-on / dim-off ANSI",
        "\x1b[2m" in first and "\x1b[22m" in first,
        repr(first),
    )
    _check(
        "second delta walks back over the prior 1-row render",
        "\x1b[1F\x1b[J" in second,
        repr(second),
    )
    _check(
        "second delta re-emits the cumulative text (Hello!)",
        "Hello!" in second,
        repr(second),
    )
    _check(
        "_streaming_text accumulated across deltas",
        cli._streaming_text["root"] == "Hello!",
        cli._streaming_text["root"],
    )
    # Cleanup
    cli._streaming_active.clear()
    cli._streaming_text.clear()
    cli._streaming_rendered_rows.clear()


def _check_cursor_advance_rows_math() -> None:
    # No text → 0 rows.
    _check("empty text → 0 advance", cli._cursor_advance_rows("", 80) == 0)
    # Single short line → 0 rows (cursor still on same row).
    _check("'hi' → 0 advance", cli._cursor_advance_rows("hi", 80) == 0)
    # Trailing \n → 1 row.
    _check("'hi\\n' → 1 advance", cli._cursor_advance_rows("hi\n", 80) == 1)
    # Soft-wrap: 80-char line on 80-wide terminal → 1 row.
    _check(
        "80-char line on width 80 → 1 advance (wrap)",
        cli._cursor_advance_rows("x" * 80, 80) == 1,
    )
    # Two newlines → 2 rows.
    _check(
        "'a\\nb\\n' → 2 advance",
        cli._cursor_advance_rows("a\nb\n", 80) == 2,
    )


def _check_non_streaming_assistant_text_still_renders_markdown() -> None:
    # Nothing in `_streaming_active` → closing assistant_text takes
    # the non-streaming branch and calls `_on_text`, which renders
    # via Markdown. Capture: at least one call carrying a Markdown
    # instance positionally.
    cli._streaming_active.clear()

    with mock.patch.object(cli.console, "print") as fake_print:
        cli._print_event({"type": "assistant_text", "text": "**hi**"})

    rendered_markdown = any(
        any(isinstance(a, cli.Markdown) for a in call.args)
        for call in fake_print.call_args_list
    )
    _check(
        "non-streaming close renders Markdown",
        rendered_markdown,
        f"calls={fake_print.call_args_list}",
    )


def _check_commit_user_line_writes_ansi() -> None:
    # Force a known terminal width so padding math is testable.
    with mock.patch.object(
        cli.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
    ):
        buf = io.StringIO()
        with mock.patch.object(sys, "stdout", buf):
            cli._commit_user_line("hello world")
        out = buf.getvalue()
    _check(
        "commit walks back over divider + arrow (\\x1b[2F\\x1b[J)",
        "\x1b[2F\x1b[J" in out,
        repr(out),
    )
    _check(
        "commit writes the │ bar + the user's line",
        "│" in out and "hello world" in out,
        repr(out),
    )
    _check(
        "commit applies a dark-grey background (48;5;236) and resets it (49)",
        "\x1b[48;5;236m" in out and "\x1b[49m" in out,
        repr(out),
    )
    _check(
        "commit bolds the user's text only (1 then 22)",
        "\x1b[1mhello world\x1b[22m" in out,
        repr(out),
    )
    _check(
        "commit pads the row so the background fills the terminal width",
        # Width 80 → "│ hello world" is 13 visible cols, pad = 80-13-1 = 66.
        " " * 66 in out,
        repr(out),
    )
    _check(
        "commit ends with a newline so the next prompt redraws below",
        out.endswith("\n"),
        repr(out),
    )

    # Empty input → erase only, no historical row written.
    with mock.patch.object(
        cli.shutil, "get_terminal_size", return_value=os.terminal_size((80, 24))
    ):
        buf2 = io.StringIO()
        with mock.patch.object(sys, "stdout", buf2):
            cli._commit_user_line("")
        out2 = buf2.getvalue()
    _check(
        "empty input erases divider + arrow",
        out2 == "\x1b[2F\x1b[J",
        repr(out2),
    )
    _check(
        "empty input writes no historical row (no │, no bg color)",
        "│" not in out2 and "\x1b[48;5;236m" not in out2,
        repr(out2),
    )


def _check_prompt_message_always_has_divider() -> None:
    """The divider used to be hidden during busy turns; now it
    always renders as the visual seam between scrollback and the
    live input area. Regression check: no busy/idle branching."""
    msg = cli._prompt_message()
    text = str(msg.value if hasattr(msg, "value") else msg)
    # ANSI prompt is wrapped — check for the divider character + arrow.
    _check(
        "prompt always includes the ─ divider",
        "─" in text,
        repr(text),
    )
    _check(
        "prompt always ends with the > arrow",
        text.rstrip().endswith(">"),
        repr(text),
    )


def _check_tool_call_renders_visibly_and_accumulates() -> None:
    """Tool-call lines should print with a cyan ⏵ marker so the user
    can scan a turn for what fired, and root-agent calls should
    accumulate in `_turn_tool_calls` for the end-of-turn summary."""
    cli._turn_tool_calls.clear()
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._on_tool_call(
            "create_memory",
            {"name": "memory_x"},
            agent_id=None,
        )
    line = fake_print.call_args.args[0]
    _check(
        "tool call renders cyan ⏵ marker + name",
        "[cyan]⏵ create_memory[/cyan]" in line,
        repr(line),
    )
    _check(
        "tool call carries dim args summary",
        "[dim]name=memory_x[/dim]" in line,
        repr(line),
    )
    _check(
        "root-agent call appended to summary accumulator",
        cli._turn_tool_calls == [{"name": "create_memory", "ok": True}],
        repr(cli._turn_tool_calls),
    )

    # Subagent call: visible, but NOT in the root accumulator.
    cli._turn_tool_calls.clear()
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._on_tool_call("glob", {"pattern": "*.py"}, agent_id="researcher-1")
    _check(
        "subagent call does NOT feed root summary",
        cli._turn_tool_calls == [],
        repr(cli._turn_tool_calls),
    )


def _check_tool_result_renders_success_and_error() -> None:
    """Successful tool results should print as `↳ preview` in
    dim cyan. Errors stay dim red AND flip the matching call's
    status in the accumulator."""
    cli._turn_tool_calls[:] = [{"name": "glob", "ok": True}]

    # Success path: prints dim cyan ↳, doesn't touch accumulator.
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._on_tool_result("glob", "file_a.py\nfile_b.py\n", agent_id=None)
    line = fake_print.call_args.args[0]
    _check(
        "successful result renders dim cyan ↳",
        "[dim cyan]↳ file_a.py[/dim cyan]" in line,
        repr(line),
    )
    _check(
        "successful result leaves accumulator status alone",
        cli._turn_tool_calls == [{"name": "glob", "ok": True}],
        repr(cli._turn_tool_calls),
    )

    # Error path: prints dim red, flips accumulator entry to ok=False.
    cli._turn_tool_calls[:] = [{"name": "glob", "ok": True}]
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._on_tool_result(
            "glob", "Error: no matches\n", agent_id=None
        )
    line = fake_print.call_args.args[0]
    _check(
        "error result renders dim red ↳",
        "[dim red]↳ Error: no matches[/dim red]" in line,
        repr(line),
    )
    _check(
        "error result flips matching call's ok=False",
        cli._turn_tool_calls == [{"name": "glob", "ok": False}],
        repr(cli._turn_tool_calls),
    )

    # LIFO match for repeated names: only the most recent still-OK
    # entry of that name flips.
    cli._turn_tool_calls[:] = [
        {"name": "glob", "ok": True},
        {"name": "glob", "ok": True},
    ]
    with mock.patch.object(cli.console, "print"):
        cli._on_tool_result("glob", "Error: x", agent_id=None)
    _check(
        "LIFO error match flips only the most recent matching call",
        cli._turn_tool_calls == [
            {"name": "glob", "ok": True},
            {"name": "glob", "ok": False},
        ],
        repr(cli._turn_tool_calls),
    )

    # Result preview truncates past 80 visible chars.
    cli._turn_tool_calls.clear()
    long_first_line = "x" * 200
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._on_tool_result("read_file", long_first_line, agent_id=None)
    line = fake_print.call_args.args[0]
    _check(
        "long result line truncated to 79 chars + ellipsis",
        "x" * 79 + "…" in line and "x" * 80 not in line,
        repr(line)[:120],
    )


def _check_turn_summary_renders_and_clears() -> None:
    """End-of-turn summary lists every root-agent tool call with
    ✓/✗ markers and clears the accumulator after rendering. No-op
    when no tools fired."""
    cli._turn_tool_calls[:] = [
        {"name": "create_memory", "ok": True},
        {"name": "glob", "ok": False},
        {"name": "execute", "ok": True},
    ]
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._render_turn_tool_summary()
    line = fake_print.call_args.args[0]
    _check(
        "summary renders all three tools with ✓/✗ markers",
        "create_memory ✓" in line
        and "glob ✗" in line
        and "execute ✓" in line,
        repr(line),
    )
    _check(
        "summary uses the dim style + 'tools:' prefix",
        line.startswith("[dim]tools:") and line.endswith("[/dim]"),
        repr(line),
    )
    _check(
        "summary clears the accumulator",
        cli._turn_tool_calls == [],
        repr(cli._turn_tool_calls),
    )

    # No-op when nothing fired.
    with mock.patch.object(cli.console, "print") as fake_print:
        cli._render_turn_tool_summary()
    _check(
        "no-tool turn → summary prints nothing",
        fake_print.call_count == 0,
        f"got {fake_print.call_count}",
    )


def main() -> None:
    _check_streaming_assistant_text_walks_back_and_renders_markdown()
    _check_non_streaming_assistant_text_still_renders_markdown()
    _check_on_text_delta_renders_cumulatively()
    _check_cursor_advance_rows_math()
    _check_commit_user_line_writes_ansi()
    _check_prompt_message_always_has_divider()
    _check_tool_call_renders_visibly_and_accumulates()
    _check_tool_result_renders_success_and_error()
    _check_turn_summary_renders_and_clears()
    print("smoke_cli_render: all checks passed")


if __name__ == "__main__":
    main()
