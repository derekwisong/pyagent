"""System prompt assembly from per-section markdown files.

The agent calls `SystemPromptBuilder.build()` at the start of every
run, so edits to SOUL.md / TOOLS.md / PRIMER.md / USER.md take effect
on the next turn without restarting the process.

USER.md is auto-loaded (resolved via `paths.resolve`) so the agent
always has the latest user notes in context. MEMORY.md is *not*
auto-loaded — the agent reads it on demand via the ledger tools.

The skills catalog can be supplied as a callable, in which case it is
re-rendered every turn — letting newly-installed or freshly-authored
skills appear without restarting.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from pyagent import paths


class SystemPromptBuilder:
    """Assembles the system prompt: persona files + auto-loaded USER
    ledger + a small footer naming the persona files' on-disk paths
    so the agent can find them when the user explicitly asks to
    edit them.
    """

    def __init__(
        self,
        soul: str | Path,
        tools: str | Path,
        primer: str | Path,
        skills_catalog: str | Callable[[], str] = "",
    ) -> None:
        self.soul = Path(soul)
        self.tools = Path(tools)
        self.primer = Path(primer)
        self.skills_catalog = skills_catalog

    def build(self) -> str:
        sections = [
            self.soul.read_text(),
            self.tools.read_text(),
            self.primer.read_text(),
        ]
        catalog = (
            self.skills_catalog()
            if callable(self.skills_catalog)
            else self.skills_catalog
        )
        if catalog:
            sections.append(catalog)
        user_path = paths.resolve("USER.md")
        if user_path.exists():
            sections.append(user_path.read_text())
        sections.append(self._persona_footer())
        return "\n\n".join(s.rstrip() for s in sections)

    def _persona_footer(self) -> str:
        return (
            "## Environment\n"
            f"- cwd: {os.getcwd()}\n"
            "\n"
            "## Where your persona lives\n"
            "Your SOUL, TOOLS, and PRIMER are loaded from the paths "
            "below. They define who you are. Don't edit them on your "
            "own initiative — self-modification without an ask is "
            "drift, not a feature. When the user asks (even casually), "
            "go ahead.\n"
            f"- SOUL:   {self.soul.resolve()}\n"
            f"- TOOLS:  {self.tools.resolve()}\n"
            f"- PRIMER: {self.primer.resolve()}"
        )
