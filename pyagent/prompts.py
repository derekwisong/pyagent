"""System prompt assembly from per-section markdown files plus
plugin-contributed sections.

The agent calls `SystemPromptBuilder.build_segments(ctx)` at the start
of every turn, so edits to SOUL.md / TOOLS.md / PRIMER.md take effect
on the next turn without restarting the process.

Layered output (in order):

  1. SOUL.md        — universal voice + core directives
  2. TOOLS.md       — operating principles
  3. PRIMER.md      — workspace, shell, subagent guidance
  4. role_body      — role-specific persona (subagents only)
  5. skills_catalog — live-rendered list of available skills
  6. roles_catalog  — live-rendered list of available subagent roles
  7. task_body      — spawn-time task description (subagents only)
  8. plugin sections (non-volatile, in registration order) — these
                      live inside the prompt-cache breakpoint so they
                      stay warm across turns
  9. footer         — persona file paths

  ── cache breakpoint ──

 10. plugin sections (volatile, in registration order) — these live
                      AFTER the breakpoint so their content can change
                      turn-to-turn without invalidating the cached
                      system block

User-facing memory (USER.md / MEMORY.md) is contributed by the
bundled `memory-markdown` plugin's prompt sections, not auto-loaded
here. Disabling that plugin removes its sections cleanly.
"""

from __future__ import annotations

import os
import platform
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pyagent.plugins import LoadedPlugins, PromptContext


class SystemPromptBuilder:
    """Assembles the system prompt: persona files + plugin sections +
    auto-loaded USER ledger + footer.

    `build_segments(ctx)` returns a `(stable, volatile)` tuple so LLM
    clients can place a cache breakpoint between them. `build()` is
    retained for callers that don't care about cache placement; it
    just concatenates the two segments.
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
        plugin_loader: "LoadedPlugins | None" = None,
    ) -> None:
        self.soul = Path(soul)
        self.tools = Path(tools)
        self.primer = Path(primer)
        self.skills_catalog = skills_catalog
        self.roles_catalog = roles_catalog
        self.role_body = role_body
        self.task_body = task_body
        self.plugin_loader = plugin_loader

    def build(self, ctx: "PromptContext | None" = None) -> str:
        """Concatenated stable+volatile segments. Use only when cache
        placement doesn't matter (e.g. tests, legacy callers)."""
        stable, volatile = self.build_segments(ctx)
        if volatile:
            return f"{stable}\n\n{volatile}"
        return stable

    def build_segments(
        self, ctx: "PromptContext | None" = None
    ) -> tuple[str, str]:
        """Return (stable, volatile) — the two halves of the system
        prompt around the cache breakpoint."""
        from pyagent.plugins import PromptContext as _PromptContext

        if ctx is None:
            ctx = _PromptContext()

        sections: list[str] = [
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

        # Plugin-contributed sections, split by `volatile`.
        volatile_sections: list[str] = []
        if self.plugin_loader is not None:
            for section in self.plugin_loader.sections():
                try:
                    rendered = section.renderer(ctx)
                except Exception:
                    # A renderer raising is the plugin's bug; log and
                    # skip its contribution this turn rather than
                    # wedging the agent.
                    import logging

                    logging.getLogger(__name__).exception(
                        "plugin %s prompt section %r renderer raised",
                        section.plugin_name,
                        section.name,
                    )
                    continue
                if not rendered:
                    continue
                if section.volatile:
                    volatile_sections.append(rendered)
                else:
                    sections.append(rendered)

        # USER ledger auto-load was here pre-plugin; now owned by the
        # memory-markdown plugin's "user-ledger" prompt section. With
        # the plugin disabled, USER content does not appear in the
        # system prompt at all — that is the clean-replacement
        # contract.

        sections.append(self._persona_footer())

        stable = "\n\n".join(s.rstrip() for s in sections)
        volatile = "\n\n".join(s.rstrip() for s in volatile_sections)
        return stable, volatile

    def _persona_footer(self) -> str:
        today = date.today()
        shell_path = os.environ.get("SHELL") or os.environ.get("COMSPEC") or ""
        shell = Path(shell_path).name if shell_path else "unknown"
        os_label = f"{platform.system()} {platform.release()}".strip() or "unknown"
        return (
            "## Environment\n"
            f"- cwd: {os.getcwd()}\n"
            f"- date: {today.isoformat()} ({today.strftime('%A')})\n"
            f"- os: {os_label}\n"
            f"- shell: {shell}\n"
            f"- python: {platform.python_version()}\n"
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
