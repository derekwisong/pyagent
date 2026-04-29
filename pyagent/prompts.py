"""System prompt assembly from per-section markdown files.

The agent calls `SystemPromptBuilder.build()` at the start of every
run, so edits to SOUL.md / TOOLS.md / PRIMER.md / USER.md take effect
on the next turn without restarting the process.

Layered output (in order):

  1. SOUL.md        — universal voice + core directives
  2. TOOLS.md       — operating principles (efficiency, errors, discretion)
  3. PRIMER.md      — workspace, shell, subagent guidance
  4. role_body      — role-specific persona (subagents only; empty for root)
  5. skills_catalog — live-rendered list of available skills
  6. roles_catalog  — live-rendered list of available subagent roles
  7. task_body      — spawn-time task description (subagents only)
  8. USER.md        — auto-loaded if it exists (user preferences)
  9. footer         — persona file paths + don't-edit notice

USER.md is auto-loaded (resolved via `paths.resolve`) so the agent
always has the latest user notes in context. MEMORY.md is *not*
auto-loaded — the agent reads it on demand via the ledger tools.

The skills and roles catalogs can be supplied as callables, in which
case they re-render every turn — letting newly-installed skills or
freshly-defined roles appear without restarting.
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
        roles_catalog: str | Callable[[], str] = "",
        role_body: str = "",
        task_body: str = "",
    ) -> None:
        self.soul = Path(soul)
        self.tools = Path(tools)
        self.primer = Path(primer)
        self.skills_catalog = skills_catalog
        self.roles_catalog = roles_catalog
        self.role_body = role_body
        self.task_body = task_body

    def build(self) -> str:
        sections = [
            self.soul.read_text(),
            self.tools.read_text(),
            self.primer.read_text(),
        ]
        if self.role_body:
            sections.append(self.role_body)
        skills = (
            self.skills_catalog()
            if callable(self.skills_catalog)
            else self.skills_catalog
        )
        if skills:
            sections.append(skills)
        roles = (
            self.roles_catalog()
            if callable(self.roles_catalog)
            else self.roles_catalog
        )
        if roles:
            sections.append(roles)
        if self.task_body:
            sections.append(self.task_body)
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
