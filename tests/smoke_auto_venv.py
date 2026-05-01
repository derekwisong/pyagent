"""Smoke for auto-venv discovery + auto-creation + pip_install (#46).

Locks:
  1. `discover` finds nothing in an empty workspace.
  2. `discover` honors `$VIRTUAL_ENV` when it points at a real venv.
  3. `discover` falls back to `<workspace>/.venv/` and then
     `<workspace>/venv/`.
  4. `discover` ignores a stale `$VIRTUAL_ENV` (variable set, no venv
     on disk) and falls back to workspace discovery.
  5. `ensure` creates `<workspace>/.venv/` when none exists, returns
     `created=True`, and a second call returns `created=False`.
  6. `pip_install` end-to-end against the real auto-created venv:
     installs a tiny pure-python package and verifies it lands in
     the venv's site-packages, NOT the host interpreter.
  7. `pip_install` returns a `<...>` marker on failure (bad spec)
     and on empty input.
  8. `describe` produces a footer-friendly one-liner for each state.

The pip install case talks to PyPI — skip if `--offline` is in
sys.argv or the network is unreachable.

Run with:

    .venv/bin/python -m tests.smoke_auto_venv
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from pyagent import tools as agent_tools
from pyagent import venv as venv_mod


def _network_ok(host: str = "pypi.org", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.gaierror):
        return False


def _make_fake_venv(target: Path) -> None:
    """Create a real venv quickly via the same mechanism `ensure`
    uses — used to seed pre-existing-venv test cases. Caller owns
    cleanup."""
    subprocess.run(
        [sys.executable, "-m", "venv", str(target)],
        check=True,
        capture_output=True,
        timeout=120,
    )


def main() -> None:
    saved_virtual_env = os.environ.pop("VIRTUAL_ENV", None)
    try:
        # 1. empty workspace → discover returns None
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            assert venv_mod.discover(ws) is None
            assert "none" in venv_mod.describe(ws), venv_mod.describe(ws)
            print("✓ empty workspace: discover() = None")

        # 2. `$VIRTUAL_ENV` points at a real venv → wins
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            outside = ws / "elsewhere"
            _make_fake_venv(outside)
            os.environ["VIRTUAL_ENV"] = str(outside)
            try:
                found = venv_mod.discover(ws)
                assert found == outside.resolve(), (found, outside.resolve())
                desc = venv_mod.describe(ws)
                assert "(active)" in desc, desc
                print(f"✓ VIRTUAL_ENV honored: {desc}")
            finally:
                os.environ.pop("VIRTUAL_ENV", None)

        # 3. workspace .venv/ — preferred over venv/ when both exist
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            _make_fake_venv(ws / ".venv")
            _make_fake_venv(ws / "venv")
            found = venv_mod.discover(ws)
            assert found == (ws / ".venv").resolve(), found
            assert "(workspace)" in venv_mod.describe(ws)
            print(f"✓ .venv/ preferred over venv/: {found.name}")

        # 3b. venv/ used when only it exists
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            _make_fake_venv(ws / "venv")
            found = venv_mod.discover(ws)
            assert found == (ws / "venv").resolve(), found
            print(f"✓ venv/ fallback: {found.name}")

        # 4. stale `$VIRTUAL_ENV` ignored, workspace fallback used
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            _make_fake_venv(ws / ".venv")
            os.environ["VIRTUAL_ENV"] = str(ws / "deleted-elsewhere")
            try:
                found = venv_mod.discover(ws)
                assert found == (ws / ".venv").resolve(), found
                print(f"✓ stale VIRTUAL_ENV ignored, fell back to workspace .venv/")
            finally:
                os.environ.pop("VIRTUAL_ENV", None)

        # 5. ensure creates on first call, idempotent on second
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            venv_path, created = venv_mod.ensure(ws)
            assert created is True, created
            assert venv_path == (ws / ".venv").resolve(), venv_path
            assert venv_mod.is_venv(venv_path), venv_path
            assert venv_mod.pip_path(venv_path).exists()

            # Second call: discover returns the same path, created=False
            venv_path2, created2 = venv_mod.ensure(ws)
            assert venv_path2 == venv_path, (venv_path2, venv_path)
            assert created2 is False, created2
            print(f"✓ ensure(): created={True}, then idempotent")

        # 6. pip_install end-to-end (skip if offline)
        if "--offline" in sys.argv or not _network_ok():
            print("⊘ pip_install live test skipped (offline / --offline)")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                # `six` is tiny, pure-python, no compiled deps —
                # ideal for a smoke that needs to land on PyPI.
                tool = agent_tools.make_pip_install(ws)
                result = tool("six")
                assert "installed" in result.lower(), result
                assert ".venv" in result, result
                print(f"✓ pip_install ran: {result.splitlines()[0]!r}")

                # Verify the package landed in the venv, not anywhere else.
                py = venv_mod.python_path(ws / ".venv")
                proc = subprocess.run(
                    [str(py), "-c", "import six; print(six.__file__)"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert proc.returncode == 0, proc.stderr
                assert ".venv" in proc.stdout, proc.stdout
                print(f"✓ six landed inside the venv: {proc.stdout.strip()}")

        # 6b. pip_install honors the optional `venv` argument:
        # relative path resolved against workspace, absolute path
        # used as-is, both auto-created when missing.
        if "--offline" in sys.argv or not _network_ok():
            print("⊘ pip_install custom-venv test skipped (offline)")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                tool = agent_tools.make_pip_install(ws)

                # Relative path: lands inside workspace
                result = tool("six", venv=".venv-test")
                assert "installed" in result.lower(), result
                rel_target = (ws / ".venv-test").resolve()
                assert str(rel_target) in result, result
                assert venv_mod.is_venv(rel_target), rel_target
                assert not (ws / ".venv").exists(), (
                    "default .venv should NOT have been created when "
                    "an explicit venv arg was given"
                )
                print(f"✓ pip_install(venv='.venv-test') → {rel_target.name}/")

                # Absolute path: explicit, outside the workspace
                with tempfile.TemporaryDirectory() as tmp2:
                    abs_target = Path(tmp2) / "tools-venv"
                    result = tool("six", venv=str(abs_target))
                    assert "installed" in result.lower(), result
                    assert venv_mod.is_venv(abs_target), abs_target
                    print(f"✓ pip_install(venv=<abs>) → {abs_target}")

        # 7a. empty spec rejected
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tool = agent_tools.make_pip_install(ws)
            assert tool("   ") == "<refused: empty package spec>"
            assert tool("") == "<refused: empty package spec>"
            print("✓ empty spec rejected without touching the venv")
            assert not (ws / ".venv").exists(), "venv created on empty input"
            print("✓ empty spec did not auto-create venv")

        # 7b. failing install returns marker (use a clearly-bogus spec)
        if "--offline" in sys.argv or not _network_ok():
            print("⊘ failing-install marker test skipped (offline)")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                ws = Path(tmp)
                tool = agent_tools.make_pip_install(ws)
                # A name that's syntactically valid but doesn't exist
                # on PyPI, so we don't accidentally match a real package.
                bogus = "pyagent-smoke-package-that-definitely-does-not-exist-xyzzy"
                result = tool(bogus)
                assert result.startswith("<pip install"), result
                assert "failed" in result, result
                print(f"✓ bad spec yields marker: {result.splitlines()[0]!r}")

        # 8. describe variants — already covered above; just one more
        # confirming the `none` form when nothing exists.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            assert "none" in venv_mod.describe(ws)
            print("✓ describe(empty) → 'none'")
    finally:
        # Restore VIRTUAL_ENV exactly as it was (or stay unset if it wasn't).
        if saved_virtual_env is not None:
            os.environ["VIRTUAL_ENV"] = saved_virtual_env

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
