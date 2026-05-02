"""Smoke for the streamed-text cursor-advance counter that drives
the end-of-turn markdown re-render in `pyagent.cli`.

The CLI streams plain dim text as deltas arrive, then on the closing
``assistant_text`` event clears the streamed region and re-renders
the same buffer as markdown so bold/headers/code blocks formatting
survives. Clearing relies on knowing how many rows the cursor moved
during streaming — a function of the buffer's hard newlines plus the
soft wraps each segment incurs against the terminal width. This file
exercises that pure function across the cases the live render path
depends on.

CLI integration is harder to test without a real TTY (the rest of
the streaming surface is covered by ``smoke_streaming.py``); the
visual reflow is verified manually with ``pyagent --model
ollama/<model>``.

Run with:

    .venv/bin/python -m tests.smoke_streamed_rerender
"""

from __future__ import annotations

from pyagent.cli import _count_cursor_advance


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_empty_and_degenerate() -> None:
    _check("empty buffer → 0 advance", _count_cursor_advance("", 80) == 0)
    _check(
        "non-positive width → 0 advance (defensive)",
        _count_cursor_advance("abc", 0) == 0,
    )
    _check(
        "negative width → 0 advance",
        _count_cursor_advance("abc", -5) == 0,
    )


def _check_short_text_no_wrap() -> None:
    """Cursor stays on the same row for short text without newlines."""
    _check(
        "short non-wrapping text → 0 advance",
        _count_cursor_advance("abc", 80) == 0,
    )


def _check_hard_newlines() -> None:
    _check(
        "trailing newline → 1 advance",
        _count_cursor_advance("abc\n", 80) == 1,
    )
    _check(
        "two segments separated by one newline → 1 advance",
        _count_cursor_advance("abc\ndef", 80) == 1,
    )
    _check(
        "three segments → 2 advances",
        _count_cursor_advance("a\nb\nc", 80) == 2,
    )
    _check(
        "purely-whitespace newlines preserved",
        _count_cursor_advance("\n\n\n", 80) == 3,
    )


def _check_soft_wrap_boundary() -> None:
    """Soft-wrap boundary cases — the formula is `(L-1) // W` so
    exactly W chars do NOT wrap (cursor sits at end of row), but
    W+1 wraps to a second row."""
    _check(
        "exactly width-many chars → no soft wrap",
        _count_cursor_advance("x" * 80, 80) == 0,
    )
    _check(
        "width+1 chars → 1 soft wrap",
        _count_cursor_advance("x" * 81, 80) == 1,
    )
    _check(
        "2*width chars → 1 soft wrap",
        _count_cursor_advance("x" * 160, 80) == 1,
    )
    _check(
        "2*width+1 chars → 2 soft wraps",
        _count_cursor_advance("x" * 161, 80) == 2,
    )


def _check_mixed_hard_and_soft() -> None:
    """A long line followed by a hard newline followed by another
    long line: each long line contributes its soft-wrap count, plus
    the hard newline contributes 1."""
    width = 40
    buf = ("x" * 100) + "\n" + ("y" * 50)
    # First segment: 100 chars on width 40 → (100-1)//40 = 2 soft wraps
    # Hard newline: +1
    # Second segment: 50 chars → (50-1)//40 = 1 soft wrap
    # Total: 2 + 1 + 1 = 4
    _check(
        f"mixed soft-and-hard wrap → 4 advance (got {_count_cursor_advance(buf, width)})",
        _count_cursor_advance(buf, width) == 4,
    )


def _check_ansi_codes_stripped() -> None:
    """Models occasionally emit ANSI escape sequences inline. Without
    stripping, those bytes inflate the visible-length count and we'd
    over-count cursor advance."""
    # 80 'x' chars wrapped in dim/reset SGR codes — visible length
    # is still 80, so no soft wrap.
    buf = "\x1b[2m" + ("x" * 80) + "\x1b[0m"
    _check(
        "ANSI-wrapped text counted by visible length",
        _count_cursor_advance(buf, 80) == 0,
        f"buffer-len={len(buf)} visible-len=80 → got {_count_cursor_advance(buf, 80)}",
    )

    # Width=10, two ANSI codes around 30 visible chars + a newline.
    buf2 = "\x1b[2m" + "x" * 30 + "\x1b[0m" + "\n" + "y" * 5
    # First seg visible 30 → (30-1)//10 = 2 soft wraps
    # Hard \n: +1
    # Second seg visible 5 → 0 soft wraps
    # Total: 3
    _check(
        "ANSI in mid-buffer doesn't bias count",
        _count_cursor_advance(buf2, 10) == 3,
        f"got {_count_cursor_advance(buf2, 10)}",
    )


def main() -> None:
    _check_empty_and_degenerate()
    _check_short_text_no_wrap()
    _check_hard_newlines()
    _check_soft_wrap_boundary()
    _check_mixed_hard_and_soft()
    _check_ansi_codes_stripped()
    print("smoke_streamed_rerender: all checks passed")


if __name__ == "__main__":
    main()
