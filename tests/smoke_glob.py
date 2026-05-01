"""Smoke for glob.

Exercises (in-process):
  1. Single-pattern recursive match returns sorted relative paths.
  2. Multi-pattern (list) merges and de-duplicates results.
  3. Default exclusions skip `.git` / `__pycache__` / `node_modules`.
  4. Limit truncation appends a `<truncated: NNN ...>` marker.
  5. Output is sorted.
  6. Permission gate refuses a `root` outside the workspace.
  7. Missing root surfaces `<path not found: ...>`.
  8. File-as-root surfaces `<not a directory: ...>`.
  9. Empty pattern list / wrong type returns an `<error: ...>` marker.
 10. Bad limit returns an `<error: ...>` marker.
 11. Relative paths are relative to `root`, not the cwd.

Run with:

    .venv/bin/python -m tests.smoke_glob
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent import permissions
from pyagent.tools import glob


def _seed(tmp: Path) -> None:
    """Build a tree exercising real and excluded paths."""
    (tmp / "src").mkdir()
    (tmp / "src" / "alpha.py").write_text("x")
    (tmp / "src" / "beta.py").write_text("x")
    (tmp / "src" / "gamma.pyi").write_text("x")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_alpha.py").write_text("x")
    (tmp / "README.md").write_text("x")
    # Excluded trees — must never appear in default-exclusion runs.
    (tmp / ".git").mkdir()
    (tmp / ".git" / "config").write_text("x")
    (tmp / ".git" / "HEAD").write_text("x")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "cached.pyc").write_text("x")
    (tmp / "node_modules").mkdir()
    (tmp / "node_modules" / "pkg").mkdir()
    (tmp / "node_modules" / "pkg" / "index.js").write_text("x")
    # Loose .pyc (file-pattern exclude, not just dir-name).
    (tmp / "src" / "stale.pyc").write_text("x")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-glob-")).resolve()
    permissions.set_workspace(tmp)
    _seed(tmp)

    # 1. Single-pattern recursive match.
    out = glob("**/*.py", root=str(tmp))
    assert out == ["src/alpha.py", "src/beta.py", "tests/test_alpha.py"], out
    print(f"✓ single-pattern recursive match: {out}")

    # 2. Multi-pattern merges and de-duplicates.
    out = glob(["**/*.py", "**/*.pyi"], root=str(tmp))
    assert out == [
        "src/alpha.py",
        "src/beta.py",
        "src/gamma.pyi",
        "tests/test_alpha.py",
    ], out
    print(f"✓ multi-pattern merge: {out}")

    # Overlapping patterns must not double-count.
    out = glob(["**/*.py", "src/*.py"], root=str(tmp))
    assert out == ["src/alpha.py", "src/beta.py", "tests/test_alpha.py"], out
    print(f"✓ multi-pattern dedupe: {out}")

    # 3. Default exclusions: .git / __pycache__ / node_modules / *.pyc all
    # absent from a wide-open recursive match.
    out = glob("**/*", root=str(tmp))
    joined = "\n".join(out)
    assert ".git/" not in joined and "/.git" not in joined, out
    assert not any(p.startswith(".git") for p in out), out
    assert not any("__pycache__" in p for p in out), out
    assert not any(p.startswith("node_modules") for p in out), out
    assert not any(p.endswith(".pyc") for p in out), out
    # Real files still present.
    assert "README.md" in out, out
    assert "src/alpha.py" in out, out
    print(f"✓ default exclusions filtered: {len(out)} entries")

    # 4. Limit truncation marker.
    out = glob("**/*.py", root=str(tmp), limit=2)
    assert len(out) == 3, out  # 2 hits + 1 marker
    assert out[0] == "src/alpha.py" and out[1] == "src/beta.py", out
    assert out[2].startswith("<truncated:"), out
    assert "3 total matches" in out[2], out
    assert "tighten the pattern" in out[2], out
    print(f"✓ limit truncation marker: {out[2]!r}")

    # 5. Sorted output (already covered by 1/2/3 equality, plus an
    # explicit lexical-order check on a known set).
    out = glob("**/*.py", root=str(tmp))
    assert out == sorted(out), out
    print(f"✓ sorted output")

    # 6. Permission gate: root outside the workspace.
    outside = Path(tempfile.mkdtemp(prefix="pyagent-smoke-glob-outside-")).resolve()
    (outside / "leaked.py").write_text("x")
    out = glob("*.py", root=str(outside))
    assert len(out) == 1, out
    assert out[0].startswith("<permission denied (outside workspace)"), out
    print(f"✓ workspace-gate refusal: {out[0]!r}")

    # 7. Missing root surfaces a marker.
    out = glob("*.py", root=str(tmp / "no-such-dir"))
    assert len(out) == 1 and out[0].startswith("<path not found:"), out
    print(f"✓ missing root marker: {out[0]!r}")

    # 8. File-as-root surfaces a marker.
    out = glob("*.py", root=str(tmp / "README.md"))
    assert len(out) == 1 and out[0].startswith("<not a directory:"), out
    print(f"✓ not-a-directory marker: {out[0]!r}")

    # 9. Empty list / wrong type.
    out = glob([], root=str(tmp))
    assert len(out) == 1 and out[0] == "<error: pattern list is empty>", out
    print(f"✓ empty pattern list: {out[0]!r}")

    out = glob(123, root=str(tmp))  # type: ignore[arg-type]
    assert len(out) == 1 and out[0].startswith("<error: pattern must be"), out
    print(f"✓ wrong pattern type: {out[0]!r}")

    # 10. Bad limit.
    out = glob("*.py", root=str(tmp), limit=0)
    assert len(out) == 1 and out[0].startswith("<error: limit must be positive"), out
    print(f"✓ non-positive limit: {out[0]!r}")

    out = glob("*.py", root=str(tmp), limit="not-a-number")  # type: ignore[arg-type]
    assert len(out) == 1 and out[0].startswith("<error: limit must be an integer"), out
    print(f"✓ non-int limit: {out[0]!r}")

    # 11. Paths are relative to `root`, not cwd. Searching with
    # root=tmp/src should yield bare filenames, not "src/alpha.py".
    out = glob("*.py", root=str(tmp / "src"))
    assert out == ["alpha.py", "beta.py"], out
    print(f"✓ paths relative to root: {out}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
