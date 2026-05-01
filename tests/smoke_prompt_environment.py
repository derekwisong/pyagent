"""Smoke test for the system-prompt environment footer.

Asserts the persona footer surfaces enough host context (OS, shell,
python) for the agent to pick the right shell idioms.

Run with:

    .venv/bin/python -m tests.smoke_prompt_environment
"""

from __future__ import annotations

import os
import platform
import tempfile
from pathlib import Path

from pyagent.prompts import SystemPromptBuilder


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        soul = root / "SOUL.md"
        tools = root / "TOOLS.md"
        primer = root / "PRIMER.md"
        soul.write_text("soul")
        tools.write_text("tools")
        primer.write_text("primer")

        builder = SystemPromptBuilder(soul=soul, tools=tools, primer=primer)
        stable, _volatile = builder.build_segments()

    assert "## Environment" in stable, stable
    assert "- cwd:" in stable, stable
    assert "- date:" in stable, stable
    assert "- os:" in stable, stable
    assert "- shell:" in stable, stable
    assert "- python:" in stable, stable

    assert platform.system() in stable, stable
    assert platform.python_version() in stable, stable

    expected_shell = (
        Path(os.environ.get("SHELL") or os.environ.get("COMSPEC") or "").name
        or "unknown"
    )
    assert f"- shell: {expected_shell}" in stable, stable

    print("✓ environment footer renders os/shell/python")


if __name__ == "__main__":
    main()
