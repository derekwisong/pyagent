"""Smoke for write_file's append mode.

Exercises (in-process):
  1. append=True creates a fresh file when one is missing.
  2. append=True concatenates onto an existing file.
  3. append=True still distinguishes its return message from
     overwrites ("Appended" vs "Wrote").
  4. The pre-existing error markers still fire under append mode
     (parent missing, is-a-directory).
  5. Default append=False still overwrites.

Run with:

    .venv/bin/python -m tests.smoke_write_file_append
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent import permissions
from pyagent.tools import write_file


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-append-"))
    permissions.set_workspace(tmp)

    # 1. append to a fresh path creates the file
    fresh = tmp / "fresh.txt"
    out = write_file(str(fresh), "alpha\n", append=True)
    assert out.startswith("Appended"), out
    assert fresh.read_text() == "alpha\n", fresh.read_text()
    print(f"✓ append creates fresh file: {out!r}")

    # 2. append concatenates onto an existing file
    out = write_file(str(fresh), "beta\n", append=True)
    assert out.startswith("Appended"), out
    assert fresh.read_text() == "alpha\nbeta\n", fresh.read_text()
    print(f"✓ append concatenates: {fresh.read_text()!r}")

    # 3. default (no append) still overwrites
    out = write_file(str(fresh), "gamma\n")
    assert out.startswith("Wrote"), out
    assert fresh.read_text() == "gamma\n", fresh.read_text()
    print(f"✓ overwrite default unchanged: {fresh.read_text()!r}")

    # 4. parent missing — append mode still surfaces the marker
    missing_parent = tmp / "no-such-dir" / "child.txt"
    out = write_file(str(missing_parent), "x", append=True)
    assert out.startswith("<parent directory does not exist"), out
    print(f"✓ parent-missing error preserved: {out!r}")

    # 5. is-a-directory — append mode still surfaces the marker
    a_dir = tmp / "subdir"
    a_dir.mkdir()
    out = write_file(str(a_dir), "x", append=True)
    assert out.startswith("<is a directory"), out
    print(f"✓ is-a-directory error preserved: {out!r}")

    # 6. byte counts match the input length
    counter = tmp / "counter.txt"
    write_file(str(counter), "")
    out = write_file(str(counter), "12345", append=True)
    assert "5 bytes" in out, out
    print(f"✓ byte count: {out!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
