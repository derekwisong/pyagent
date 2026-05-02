"""Smoke tests for the py-dev-toolkit plugin (lint / typecheck / run_pytest).

Each test is gated on the relevant binary being installed in the
runtime environment. CI hosts without ruff / mypy / pytest will skip
the matching block rather than fail — same shape as the rest of the
plugin smoke suite. The plugin's own missing-tool error path is
covered by spoofing PATH lookup.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from pyagent import permissions
from pyagent.plugins import load


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        sys.exit(1)


def _setup() -> tuple[dict, Path]:
    permissions.set_workspace(Path.cwd())
    workdir = Path(tempfile.mkdtemp(prefix="pydevtools_smoke_"))
    permissions.pre_approve(workdir)
    loaded = load()
    tools = {name: fn for name, (_, fn) in loaded.tools().items()}
    return tools, workdir


def test_plugin_registers_three_tools(tools: dict) -> None:
    for name in ("lint", "typecheck", "run_pytest"):
        _check(f"{name!r} registered", name in tools, f"have: {sorted(tools)}")


def test_lint_findings_and_clean(tools: dict, workdir: Path) -> None:
    if shutil.which("ruff") is None:
        _check("ruff smoke skipped (binary missing)", True)
        return
    bad = workdir / "lint_bad.py"
    bad.write_text(
        "import os, sys\n"
        "unused = 42\n"
    )
    out = tools["lint"](str(bad))
    _check("lint summary line includes count", "ruff:" in out and "finding" in out, out[:120])
    _check("lint cites E401 (multi-import)", "E401" in out, out[:200])
    _check("lint marks fixable", "fixable" in out, out[:200])

    clean = workdir / "lint_clean.py"
    clean.write_text("def f() -> int:\n    return 42\n")
    out_clean = tools["lint"](str(clean))
    _check("lint clean run", out_clean.startswith("ruff: clean"), out_clean)


def test_lint_input_validation(tools: dict, workdir: Path) -> None:
    out = tools["lint"]("")
    _check("empty path → error", out.startswith("<error:"), out)

    out = tools["lint"]("/no/such/path-xyz")
    _check("missing path → error", "does not exist" in out, out)

    if (workdir / "lint_clean.py").exists():
        out = tools["lint"](str(workdir / "lint_clean.py"), tools=["pylint"])
        _check(
            "unsupported linter → error",
            "<error:" in out and "pylint" in out,
            out,
        )


def test_typecheck_mypy(tools: dict, workdir: Path) -> None:
    if shutil.which("mypy") is None:
        _check("mypy smoke skipped (binary missing)", True)
        return
    bad = workdir / "tc_bad.py"
    bad.write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "x: str = add(1, 2)\n"
    )
    out = tools["typecheck"](str(bad), tool="mypy")
    _check("mypy summary line", "mypy:" in out and "finding" in out, out[:120])
    _check("mypy cites assignment error code", "[assignment]" in out, out[:300])

    clean = workdir / "tc_clean.py"
    clean.write_text("def add(a: int, b: int) -> int:\n    return a + b\n")
    out_clean = tools["typecheck"](str(clean), tool="mypy")
    _check("mypy clean run", out_clean.startswith("mypy: clean"), out_clean)


def test_typecheck_input_validation(tools: dict) -> None:
    out = tools["typecheck"]("/some/path", tool="pyflakes")
    _check(
        "unsupported typecheck tool → error",
        "<error:" in out and "pyflakes" in out,
        out,
    )


def test_run_pytest_basic(tools: dict, workdir: Path) -> None:
    if shutil.which("pytest") is None:
        _check("pytest smoke skipped (binary missing)", True)
        return
    test_file = workdir / "test_demo.py"
    test_file.write_text(
        "def test_pass():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "def test_fail():\n"
        "    assert 1 + 1 == 3, 'math broken'\n"
        "\n"
        "def test_skip():\n"
        "    import pytest; pytest.skip('not relevant')\n"
    )
    out = tools["run_pytest"](str(test_file))
    if "pytest-json-report" in out and "<error:" in out:
        _check("pytest smoke skipped (json-report plugin missing)", True)
        return
    _check(
        "pytest reports passed/failed/skipped",
        "1 passed" in out and "1 failed" in out and "1 skipped" in out,
        out[:200],
    )
    _check(
        "pytest surfaces failed test id",
        "test_demo.py::test_fail" in out,
        out[:300],
    )


def test_run_pytest_k_filter(tools: dict, workdir: Path) -> None:
    if shutil.which("pytest") is None:
        return
    test_file = workdir / "test_demo.py"
    if not test_file.exists():
        return
    out = tools["run_pytest"](str(test_file), k="pass")
    if "pytest-json-report" in out and "<error:" in out:
        return
    # Format omits zero-counts ("0 failed" never appears), so a
    # filtered run with only passes shouldn't mention "failed" in
    # the summary half (the part before the duration parenthesis).
    _check(
        "pytest k= filter narrows to one test",
        "1 passed" in out and "failed" not in out.split("(")[0],
        out[:200],
    )


def test_mypy_text_parser_handles_windows_paths() -> None:
    """Reviewer-found bug: previous regex used `[^:]+` for the file
    path, so `C:\\foo\\bar.py:3:1: error: …` never matched and
    Windows hosts got a false-clean result. New regex uses a
    non-greedy `.+?` so drive-letter paths parse correctly."""
    from pyagent.plugins.py_dev_toolkit.typecheck import parse_mypy_text

    sample = (
        "C:\\src\\proj\\foo.py:3:1: error: Bad arg [arg-type]\n"
        "/src/proj/foo.py:5:9: error: Other thing [assignment]\n"
        "relative/foo.py:7:2: warning: Risky [misc]\n"
    )
    out = parse_mypy_text(sample)
    _check("3 findings parsed across path styles", len(out) == 3, repr(out))
    paths = {f["filename"] for f in out}
    _check(
        "windows path captured intact",
        "C:\\src\\proj\\foo.py" in paths,
        repr(paths),
    )
    _check(
        "linux abs path captured intact",
        "/src/proj/foo.py" in paths,
        repr(paths),
    )
    _check(
        "relative path captured intact",
        "relative/foo.py" in paths,
        repr(paths),
    )


def test_mypy_text_parser_filters_notes() -> None:
    """Reviewer-found bug: `note:` lines were being counted as
    findings, inflating the summary (e.g. `1 error, 4 notes` shown
    as 5 findings). They're context for the adjacent error, not
    separate problems."""
    from pyagent.plugins.py_dev_toolkit.typecheck import parse_mypy_text

    sample = (
        "/proj/foo.py:10:5: error: Incompatible overload [misc]\n"
        "/proj/foo.py:10:5: note: Possible overload variant 1\n"
        "/proj/foo.py:10:5: note: Possible overload variant 2\n"
        "/proj/foo.py:11:5: note: Suggested fix: rename param\n"
    )
    out = parse_mypy_text(sample)
    _check("only the error is kept", len(out) == 1, repr(out))
    _check(
        "kept finding is the error",
        out[0]["severity"] == "error",
        repr(out[0]),
    )


def test_mypy_json_parser_filters_notes() -> None:
    """Same notes-filtering applied to mypy's JSONL output."""
    from pyagent.plugins.py_dev_toolkit.typecheck import parse_mypy_json

    sample = (
        '{"file": "/proj/foo.py", "line": 1, "column": 1, '
        '"severity": "error", "code": "assignment", "message": "bad"}\n'
        '{"file": "/proj/foo.py", "line": 1, "column": 1, '
        '"severity": "note", "code": null, "message": "context"}\n'
        'Found 1 error in 1 file (checked 1 source file)\n'
    )
    out = parse_mypy_json(sample)
    _check("error kept, note dropped, footer ignored", len(out) == 1, repr(out))
    _check(
        "kept finding is the error",
        out[0]["severity"] == "error" and out[0]["code"] == "assignment",
        repr(out[0]),
    )


def test_lint_on_directory(tools: dict, workdir: Path) -> None:
    if shutil.which("ruff") is None:
        return
    sub = workdir / "lint_dir"
    sub.mkdir(exist_ok=True)
    (sub / "a.py").write_text("import os\n")  # F401
    (sub / "b.py").write_text("def f():\n    return 42\n")  # clean
    out = tools["lint"](str(sub))
    _check(
        "lint runs on a directory",
        "ruff:" in out and "finding" in out,
        out[:200],
    )
    _check("finding cites file in directory", "a.py" in out, out[:300])


def test_pytest_permission_gate_runs_for_nonexistent_paths(
    tools: dict,
) -> None:
    """Reviewer-found bug: `if target_path.exists() and not
    require_access(...)` skipped the permission check when the path
    didn't exist. An out-of-workspace target like `/etc/foo.py` then
    got pytest invoked anyway (its discovery walks parents).
    Regression test: a non-existent out-of-workspace path must
    surface a clear refusal, not silently invoke pytest.

    We install a deny-all prompt handler so any out-of-workspace
    path is rejected without an interactive prompt.
    """
    saved_handler = permissions._PROMPT_HANDLER  # type: ignore[attr-defined]
    permissions.set_prompt_handler(lambda _target: False)
    try:
        out = tools["run_pytest"]("/no/such/dir/test_xyz.py")
        _check(
            "pytest surfaces access-denied for non-existent OOW path",
            "<error:" in out and "access denied" in out,
            out,
        )
    finally:
        permissions.set_prompt_handler(saved_handler)


def test_missing_binary_path(workdir: Path) -> None:
    # Spoof PATH to confirm clean error when binary missing. Need a
    # real-on-disk file because the existence check runs before the
    # binary check.
    real = workdir / "stub_for_missing_binary.py"
    real.write_text("x = 1\n")
    saved = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = "/nonexistent"
        from pyagent.plugins.py_dev_toolkit import lint
        out = lint.run(str(real))
        _check(
            "lint surfaces missing-binary error cleanly",
            "<error:" in out and "ruff is not on PATH" in out,
            out,
        )
    finally:
        os.environ["PATH"] = saved


def main() -> None:
    tools, workdir = _setup()
    test_plugin_registers_three_tools(tools)
    test_lint_findings_and_clean(tools, workdir)
    test_lint_input_validation(tools, workdir)
    test_typecheck_mypy(tools, workdir)
    test_typecheck_input_validation(tools)
    test_run_pytest_basic(tools, workdir)
    test_run_pytest_k_filter(tools, workdir)
    test_mypy_text_parser_handles_windows_paths()
    test_mypy_text_parser_filters_notes()
    test_mypy_json_parser_filters_notes()
    test_lint_on_directory(tools, workdir)
    test_pytest_permission_gate_runs_for_nonexistent_paths(tools)
    test_missing_binary_path(workdir)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
