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


def test_plugin_registers_three_tools() -> None:
    tools, _ = _setup()
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
    _check(
        "pytest k= filter narrows to one test",
        "1 passed" in out and "0 failed" not in out and "failed" not in out.split("(")[0],
        out[:200],
    )


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
    test_plugin_registers_three_tools()
    test_lint_findings_and_clean(tools, workdir)
    test_lint_input_validation(tools, workdir)
    test_typecheck_mypy(tools, workdir)
    test_typecheck_input_validation(tools)
    test_run_pytest_basic(tools, workdir)
    test_run_pytest_k_filter(tools, workdir)
    test_missing_binary_path(workdir)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
