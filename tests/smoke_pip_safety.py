"""Smoke test for pip-pollution danger patterns.

Asserts `_safety_check` flags installs that would pollute system or
user-level Python and that benign pip use still passes.

Run with:

    .venv/bin/python -m tests.smoke_pip_safety
"""

from __future__ import annotations

from pyagent.tools import _safety_check


def main() -> None:
    blocked = [
        ("pip install foo --break-system-packages",
         "--break-system-packages"),
        ("pip3 install --break-system-packages requests",
         "--break-system-packages"),
        ("pip install --user httpx", "--user"),
        ("pip3 install httpx --user", "--user"),
        ("sudo pip install requests", "sudo"),
        ("sudo -H pip3 install requests", "sudo"),
    ]
    for cmd, fragment in blocked:
        reason = _safety_check(cmd)
        assert reason is not None, f"expected block, got pass: {cmd!r}"
        print(f"✓ blocked ({fragment}): {cmd!r} → {reason!r}")

    allowed = [
        ".venv/bin/pip install requests",
        ".venv/bin/pip install -r requirements.txt",
        "uv pip install httpx",
        "pip install foo",
        "pip download --user-agent custom foo",
        "git log --user.email",
    ]
    for cmd in allowed:
        reason = _safety_check(cmd)
        assert reason is None, f"expected pass, got blocked: {cmd!r} → {reason!r}"
        print(f"✓ allowed: {cmd!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
