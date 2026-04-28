"""Smoke for the CLI's _agent_label rendering.

The label has to survive rich's markup parser and produce
`[<agent_id>] ` in cyan around the brackets. A previous version used
unescaped inner brackets and rich silently swallowed them, leaving
only the trailing space — subagent events looked indistinguishable
from root events.

This test renders to a captured Console with markup enabled and
asserts the literal `[<id>]` string appears in the output.

Run with:

    .venv/bin/python -m tests.smoke_agent_label
"""

from __future__ import annotations

import io

from rich.console import Console

from pyagent.cli import _agent_label


def main() -> None:
    # Empty for root.
    assert _agent_label(None) == "", _agent_label(None)
    assert _agent_label("") == "", _agent_label("")
    print("✓ root agent_id renders empty")

    # Subagent label survives the markup parser.
    label = _agent_label("sleeper-519f719d")
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(
        f"{label}ready"
    )
    rendered = buf.getvalue().rstrip()
    assert rendered == "[sleeper-519f719d] ready", repr(rendered)
    print(f"✓ subagent label renders literally: {rendered!r}")

    # And in tool-call shape.
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(
        f"{label}[dim]· execute  command=sleep 5[/dim]"
    )
    rendered = buf.getvalue().rstrip()
    assert rendered == "[sleeper-519f719d] · execute  command=sleep 5", (
        repr(rendered)
    )
    print(f"✓ tool-call shape renders: {rendered!r}")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
