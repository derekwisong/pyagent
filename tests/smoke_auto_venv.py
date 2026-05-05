"""Smoke for venv discovery / creation + the `python_env` tool.

Locks:
  1. `discover` finds nothing in an empty workspace.
  2. `discover` IGNORES `$VIRTUAL_ENV` even when it points at a real
     venv. Pyagent's worldview is workspace + agent — an inherited
     activation from the launching shell does not bind installs.
  3. `discover` finds `<workspace>/.venv/` and prefers it over
     `<workspace>/venv/` when both exist.
  4. `ensure` creates `<workspace>/.venv/` even when `$VIRTUAL_ENV`
     points at a perfectly good venv elsewhere — the workspace
     bootstrap path is deterministic, never a fallback to the
     inherited activation.
  5. `ensure` creates `<workspace>/.venv/` when none exists, returns
     `created=True`, and a second call returns `created=False`.
  6. `python_env(scope="workspace")` end-to-end: returns JSON with
     paths + version, reports `exists_before_call=False` on the
     bootstrapping call and `True` on the second call. The created
     venv is real (has python + pip) and lives at `<workspace>/.venv`.
  7. `python_env(scope="agent")`: returns JSON describing
     `sys.executable`'s venv (or the no-venv branch when pyagent
     isn't itself in a venv). Never creates anything.
  8. `python_env` rejects an unknown scope with a `<error: …>`
     marker.
  9. `describe` produces a footer-friendly one-liner for each state
     and does NOT add an "(active)" suffix from `$VIRTUAL_ENV`.

Network is not required — no PyPI calls.

Run with:

    .venv/bin/python -m tests.smoke_auto_venv
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from pyagent import venv as venv_mod
from pyagent.plugins.py_dev_toolkit import python_env as python_env_mod


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

        # 2. `$VIRTUAL_ENV` set at a real venv → IGNORED.
        # Empty workspace + activated outside venv: discover still
        # returns None because pyagent's discovery is workspace-only.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            outside = ws / "elsewhere"
            _make_fake_venv(outside)
            os.environ["VIRTUAL_ENV"] = str(outside)
            try:
                found = venv_mod.discover(ws)
                assert found is None, (
                    f"discover should ignore VIRTUAL_ENV; got {found}"
                )
                desc = venv_mod.describe(ws)
                assert "(active)" not in desc, (
                    f"describe should not advertise VIRTUAL_ENV; got {desc}"
                )
                assert "none" in desc, desc
                print("✓ VIRTUAL_ENV ignored when workspace has no venv")
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

        # 4. `$VIRTUAL_ENV` does NOT short-circuit `ensure`. Empty
        # workspace + activated outside venv: ensure must create
        # `<workspace>/.venv/` rather than returning the inherited
        # activation.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            outside = ws / "elsewhere"
            _make_fake_venv(outside)
            os.environ["VIRTUAL_ENV"] = str(outside)
            try:
                venv_path, created = venv_mod.ensure(ws)
                assert created is True, (
                    f"ensure should create workspace venv even with "
                    f"VIRTUAL_ENV set; got created={created}"
                )
                assert venv_path == (ws / ".venv").resolve(), (
                    f"ensure should bind to workspace .venv, not "
                    f"the inherited path; got {venv_path}"
                )
                print(
                    "✓ ensure() ignores VIRTUAL_ENV and creates "
                    "workspace .venv anyway"
                )
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
            print("✓ ensure(): created=True, then idempotent")

        # 6. python_env(scope="workspace"): bootstraps + introspects.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tool = python_env_mod.make_python_env(ws)

            # First call: should create the venv and report
            # exists_before_call=False.
            raw = tool("workspace")
            assert not raw.startswith("<error"), raw
            data = json.loads(raw)
            assert data["scope"] == "workspace", data
            assert data["venv_path"] == str((ws / ".venv").resolve()), data
            assert Path(data["python"]).exists(), data
            assert Path(data["pip"]).exists(), data
            # Version should be major.minor.patch — at minimum
            # contain two dots and parse-able numeric components.
            parts = data["python_version"].split(".")
            assert len(parts) >= 3 and all(p.isdigit() for p in parts), data
            assert data["exists_before_call"] is False, data
            assert data["note"] == "", data
            print(
                "✓ python_env(workspace) bootstrapped venv: "
                f"py={data['python_version']}"
            )

            # Second call: idempotent, exists_before_call=True.
            raw2 = tool("workspace")
            data2 = json.loads(raw2)
            assert data2["venv_path"] == data["venv_path"], data2
            assert data2["exists_before_call"] is True, data2
            print("✓ python_env(workspace) second call: exists_before_call=True")

            # Default scope == "workspace"
            raw3 = tool()
            data3 = json.loads(raw3)
            assert data3["scope"] == "workspace", data3
            print("✓ python_env() defaults to scope='workspace'")

        # 7. python_env(scope="agent"): introspects sys.executable's
        # venv. Never creates. Two cases the test runner might be in:
        #   a) pyagent itself running in a venv → venv_path populated.
        #   b) running off the system interpreter → note explains it,
        #      pip is empty, exists_before_call=False.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tool = python_env_mod.make_python_env(ws)
            raw = tool("agent")
            assert not raw.startswith("<error"), raw
            data = json.loads(raw)
            assert data["scope"] == "agent", data
            assert data["python"] == sys.executable, data
            in_venv = sys.prefix != sys.base_prefix
            if in_venv:
                assert data["venv_path"], data
                assert data["exists_before_call"] is True, data
                print(
                    "✓ python_env(agent) reports active venv: "
                    f"{data['venv_path']}"
                )
            else:
                assert data["venv_path"] == "", data
                assert data["exists_before_call"] is False, data
                assert data["note"], "expected note explaining no-venv state"
                print("✓ python_env(agent) honestly reports no venv (system py)")
            # Either way, the call must NOT have created
            # `<workspace>/.venv` — agent scope never bootstraps.
            assert not (ws / ".venv").exists(), (
                "python_env(agent) should not touch the workspace"
            )

        # 8. unknown scope rejected with marker
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            tool = python_env_mod.make_python_env(ws)
            result = tool("nonsense")
            assert result.startswith("<error:"), result
            assert "scope" in result, result
            print(f"✓ unknown scope rejected: {result!r}")

        # 9. describe variants — the empty case used to say
        # "created on first pip_install"; now it points at python_env.
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            desc = venv_mod.describe(ws)
            assert "none" in desc, desc
            assert "python_env" in desc, desc
            print(f"✓ describe(empty) → {desc!r}")
    finally:
        # Restore VIRTUAL_ENV exactly as it was (or stay unset if it wasn't).
        if saved_virtual_env is not None:
            os.environ["VIRTUAL_ENV"] = saved_virtual_env

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
