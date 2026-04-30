"""Smoke for edit_file.

Exercises (in-process):
  1. Single-match replace, with correct 1-indexed line number in
     the success message.
  2. Zero-match returns an `<error: ...>` marker.
  3. Multi-match without replace_all refuses, naming the count.
  4. Multi-match with replace_all replaces every occurrence and
     reports the count.
  5. Multi-line `old_string` spanning newlines.
  6. Empty `old_string` is refused upfront.
  7. old_string == new_string is refused upfront.
  8. Missing file surfaces the standard `<file not found: ...>` marker.

Run with:

    .venv/bin/python -m tests.smoke_edit_file
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent import permissions
from pyagent.tools import edit_file


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-edit-"))
    permissions.set_workspace(tmp)

    # 1. single-match replace + line number
    p = tmp / "single.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    out = edit_file(str(p), "beta", "BETA")
    assert "replaced 1 occurrence" in out, out
    assert "line 2" in out, out
    assert p.read_text() == "alpha\nBETA\ngamma\n", p.read_text()
    print(f"✓ single-match replace: {out!r}")

    # 2. zero-match
    out = edit_file(str(p), "delta", "DELTA")
    assert out.startswith("<error: old_string not found"), out
    print(f"✓ zero-match refusal: {out!r}")

    # 3. multi-match without replace_all
    p2 = tmp / "multi.txt"
    p2.write_text("foo bar foo baz foo\n")
    out = edit_file(str(p2), "foo", "FOO")
    assert "matches 3 times" in out, out
    assert "replace_all=True" in out, out
    # File should be untouched
    assert p2.read_text() == "foo bar foo baz foo\n", p2.read_text()
    print(f"✓ multi-match refusal preserves file: {out!r}")

    # 4. multi-match with replace_all
    out = edit_file(str(p2), "foo", "FOO", replace_all=True)
    assert "replaced 3 occurrences" in out, out
    assert p2.read_text() == "FOO bar FOO baz FOO\n", p2.read_text()
    print(f"✓ replace_all: {out!r}")

    # 5. multi-line old_string
    p3 = tmp / "multiline.txt"
    p3.write_text("line1\nline2\nline3\nline4\n")
    out = edit_file(str(p3), "line2\nline3\n", "REPLACED\n")
    assert "replaced 1 occurrence" in out, out
    assert "line 2" in out, out
    assert p3.read_text() == "line1\nREPLACED\nline4\n", p3.read_text()
    print(f"✓ multi-line replace: {out!r}")

    # 6. empty old_string upfront refusal
    out = edit_file(str(p), "", "anything")
    assert out == "<error: old_string is empty>", out
    print(f"✓ empty old_string refused: {out!r}")

    # 7. old == new degenerate refusal
    out = edit_file(str(p), "alpha", "alpha")
    assert out == "<error: old_string and new_string are identical>", out
    print(f"✓ identical old/new refused: {out!r}")

    # 8. missing file
    out = edit_file(str(tmp / "no-such-file.txt"), "x", "y")
    assert out.startswith("<file not found"), out
    print(f"✓ missing file marker: {out!r}")

    # 9. replace_all on a single match — pluralization should be correct
    p_solo = tmp / "solo.txt"
    p_solo.write_text("only here\n")
    out = edit_file(str(p_solo), "only here", "ONLY", replace_all=True)
    assert "replaced 1 occurrence" in out and "occurrences" not in out, out
    print(f"✓ replace_all single-match pluralization: {out!r}")

    # 10. line number on a single-line file is 1
    p4 = tmp / "oneline.txt"
    p4.write_text("just one line, no newline at end")
    out = edit_file(str(p4), "one line", "single line")
    assert "line 1" in out, out
    assert p4.read_text() == "just single line, no newline at end", p4.read_text()
    print(f"✓ single-line line number: {out!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
