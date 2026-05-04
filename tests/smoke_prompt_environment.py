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


def _check_environment_footer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        soul = root / "SOUL.md"
        tools = root / "TOOLS.md"
        primer = root / "PRIMER.md"
        soul.write_text("PERSONA-SOUL-BODY")
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
    assert "- venv:" in stable, stable

    assert platform.system() in stable, stable
    assert platform.python_version() in stable, stable

    expected_shell = (
        Path(os.environ.get("SHELL") or os.environ.get("COMSPEC") or "").name
        or "unknown"
    )
    assert f"- shell: {expected_shell}" in stable, stable

    # Default include_soul=True: SOUL body and SOUL: <path> footer line both present.
    assert "PERSONA-SOUL-BODY" in stable, stable
    assert f"- SOUL:   {soul.resolve()}" in stable, stable

    print("✓ environment footer renders os/shell/python")


def _check_include_soul_false_skips_soul() -> None:
    """Subagents and role-invoked top-level sessions pass
    include_soul=False — SOUL body must not appear in the prompt,
    and the persona-paths footer must omit the SOUL line."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        soul = root / "SOUL.md"
        tools = root / "TOOLS.md"
        primer = root / "PRIMER.md"
        soul.write_text("PERSONA-SOUL-BODY")
        tools.write_text("TOOLS-BODY")
        primer.write_text("PRIMER-BODY")

        builder = SystemPromptBuilder(
            soul=soul, tools=tools, primer=primer, include_soul=False
        )
        stable, _volatile = builder.build_segments()

    assert "PERSONA-SOUL-BODY" not in stable, stable
    assert "TOOLS-BODY" in stable, stable
    assert "PRIMER-BODY" in stable, stable
    # Persona-paths footer omits SOUL line.
    assert "- SOUL:" not in stable, stable
    assert "- TOOLS:" in stable, stable
    assert "- PRIMER:" in stable, stable
    print("✓ include_soul=False: SOUL body + footer line both skipped")


def main() -> None:
    _check_environment_footer()
    _check_include_soul_false_skips_soul()


if __name__ == "__main__":
    main()
