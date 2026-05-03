"""Smoke for grep.

Exercises (in-process):
  1. Default behavior: bare pattern/path returns `path:lineno:line`,
     no context.
  2. `before=N` emits N leading lines per match using the dash
     separator.
  3. `after=N` emits N trailing lines per match.
  4. `context=N` is shorthand for `before=N, after=N`.
  5. Explicit `before` overrides `context` on the leading side.
  6. Match near file start/end truncates context without going OOB.
  7. Adjacent matches collapse: context lines aren't duplicated and
     no `--` separator appears inside the merged group.
  8. Multiple files: matches in different files are emitted in path
     order; per-file groups are separated by `--` only within a file.
  9. Bad input: negative / non-coercible context returns an
     `<error: ...>` marker.

Run with:

    .venv/bin/python -m tests.smoke_grep
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent import permissions
from pyagent.tools import grep


SAMPLE = """\
line 1
line 2
def helper():
    \"\"\"Compute the answer.\"\"\"
    return 42
line 6
line 7
def other():
    return 99
line 10
line 11
line 12
"""


def _seed(tmp: Path) -> Path:
    f = tmp / "sample.py"
    f.write_text(SAMPLE)
    return f


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-grep-")).resolve()
    permissions.set_workspace(tmp)
    f = _seed(tmp)
    fp = str(f)

    # 1. Default behavior — no context, colon separator only.
    out = grep(r"return", fp)
    assert out == [f"{fp}:5:    return 42", f"{fp}:9:    return 99"], out
    print(f"[ok] default behavior: {len(out)} matches")

    # 2. before=2: two leading lines per match (dash separator).
    out = grep(r"return 42", fp, before=2)
    assert out == [
        f"{fp}:3-def helper():",
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
    ], out
    print(f"[ok] before=2: {out}")

    # 3. after=2: two trailing lines per match.
    out = grep(r"return 42", fp, after=2)
    assert out == [
        f"{fp}:5:    return 42",
        f"{fp}:6-line 6",
        f"{fp}:7-line 7",
    ], out
    print(f"[ok] after=2: {out}")

    # 4. context=2 == before=2 + after=2.
    out_ctx = grep(r"return 42", fp, context=2)
    out_ba = grep(r"return 42", fp, before=2, after=2)
    assert out_ctx == out_ba, (out_ctx, out_ba)
    assert out_ctx == [
        f"{fp}:3-def helper():",
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
        f"{fp}:6-line 6",
        f"{fp}:7-line 7",
    ], out_ctx
    print(f"[ok] context=2 equals before=2,after=2")

    # 5. Explicit before wins over context. context=2, before=1 →
    # 1 leading line, 2 trailing lines.
    out = grep(r"return 42", fp, context=2, before=1)
    assert out == [
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
        f"{fp}:6-line 6",
        f"{fp}:7-line 7",
    ], out
    print(f"[ok] explicit before overrides context: {len(out)} lines")

    # 6. Truncation at boundaries. Match on line 1 with before=5
    # must not produce negative line numbers; match on last line
    # with after=5 must not exceed file length.
    edge = tmp / "edge.txt"
    edge.write_text("alpha\nbeta\ngamma\n")
    ep = str(edge)
    out = grep(r"alpha", ep, before=5, after=1)
    assert out == [f"{ep}:1:alpha", f"{ep}:2-beta"], out
    out = grep(r"gamma", ep, before=1, after=5)
    assert out == [f"{ep}:2-beta", f"{ep}:3:gamma"], out
    print(f"[ok] context truncates at file boundaries")

    # 7. Adjacent matches collapse: pattern hits both `return 42`
    # (line 5) and `return 99` (line 9). With context=2, windows
    # are [3..7] and [7..11] — they touch, so one merged group, no
    # `--` separator between them, line 7 emitted exactly once.
    out = grep(r"return", fp, context=2)
    assert out == [
        f"{fp}:3-def helper():",
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
        f"{fp}:6-line 6",
        f"{fp}:7-line 7",
        f"{fp}:8-def other():",
        f"{fp}:9:    return 99",
        f"{fp}:10-line 10",
        f"{fp}:11-line 11",
    ], out
    assert "--" not in out, out
    # Sanity: each line appears at most once.
    assert len(out) == len(set(out)), out
    print(f"[ok] adjacent matches collapse without duplication")

    # 7b. Non-adjacent matches keep the `--` separator. context=0,
    # before=1: window [4..5] then [8..9] don't touch.
    out = grep(r"return", fp, before=1)
    assert out == [
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
        "--",
        f"{fp}:8-def other():",
        f"{fp}:9:    return 99",
    ], out
    print(f"[ok] non-adjacent matches separated by --")

    # 8. Multiple files: hits in different files are emitted in
    # alphabetical path order, each file's groups separated by `--`
    # only when context is non-zero.
    other = tmp / "other.py"
    other.write_text("hello world\nreturn 7\nbye\n")
    op = str(other)
    out = grep(r"return", str(tmp), context=1)
    # Files emerge in alphabetical order: other.py, sample.py.
    # edge.txt has no "return". sample.py groups (lines 4-6, 8-10)
    # are non-adjacent (gap at line 7) so they get a `--` between
    # them. Cross-file boundaries are *not* separated by `--` (the
    # path itself signals the split).
    expected = [
        f"{op}:1-hello world",
        f"{op}:2:return 7",
        f"{op}:3-bye",
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
        f"{fp}:6-line 6",
        "--",
        f"{fp}:8-def other():",
        f"{fp}:9:    return 99",
        f"{fp}:10-line 10",
    ]
    assert out == expected, out
    print(f"[ok] multiple files emit `--` between groups")

    # 8b. Same multi-file search without context: no `--` lines at
    # all, plain colon separator throughout, sorted by file then
    # line.
    out = grep(r"return", str(tmp))
    assert out == [
        f"{op}:2:return 7",
        f"{fp}:5:    return 42",
        f"{fp}:9:    return 99",
    ], out
    assert "--" not in out, out
    print(f"[ok] no-context multi-file output stays flat")

    # 9. Bad input: negative integer / non-coercible string.
    out = grep(r"return", fp, before=-1)
    assert len(out) == 1 and out[0].startswith("<error:") and "non-negative" in out[0], out
    print(f"[ok] negative before: {out[0]!r}")

    out = grep(r"return", fp, context=-3)
    assert len(out) == 1 and "non-negative" in out[0], out
    print(f"[ok] negative context: {out[0]!r}")

    out = grep(r"return", fp, after="banana")  # type: ignore[arg-type]
    assert len(out) == 1 and out[0].startswith("<error:") and "integers" in out[0], out
    print(f"[ok] non-coercible after: {out[0]!r}")

    # Coercion: numeric string is accepted.
    out = grep(r"return 42", fp, before="2")  # type: ignore[arg-type]
    assert out == [
        f"{fp}:3-def helper():",
        f"{fp}:4-    \"\"\"Compute the answer.\"\"\"",
        f"{fp}:5:    return 42",
    ], out
    print(f"[ok] string coerces to int")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
