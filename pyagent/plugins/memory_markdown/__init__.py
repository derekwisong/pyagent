"""memory-markdown — bundled markdown ledger backend.

The original USER.md / MEMORY.md system, expressed through the v1
plugin API. Two tools, two prompt sections, one lifecycle hook.

Memory model preserved from pre-plugin pyagent:
  USER ledger    — splatted: auto-loaded into every system prompt
                   (small, always-relevant: preferences, conventions,
                   name, timezone).
  MEMORY ledger  — recalled on demand: agent calls read_ledger("MEMORY")
                   when it judges the answer might be there. Avoids
                   ballooning the prompt with potentially-large content.

Companion files in this directory:
  manifest.toml
  defaults/MEMORY.md   — seed template for the long-term memory file
  defaults/USER.md     — seed template for the per-user notes file
  defaults/PROMPT.md   — the "how to use the ledgers" instructional
                         prose, lifted from SOUL.md
"""

from __future__ import annotations

import shutil
from pathlib import Path

_LEDGERS = {"USER": "USER.md", "MEMORY": "MEMORY.md"}


def register(api):
    """Plugin entrypoint."""

    plugin_dir = Path(__file__).parent
    seeds = plugin_dir / "defaults"

    # Persistent ledger storage: <config-dir>/plugins/memory-markdown/.
    # Lazy-created on first access.
    storage = api.user_data_dir

    def _ledger_path(name: str) -> Path:
        return storage / _LEDGERS[name]

    def _seed_if_missing(name: str) -> None:
        target = _ledger_path(name)
        if target.exists():
            return
        bundled = seeds / _LEDGERS[name]
        if bundled.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(bundled, target)

    # ---- Tools ------------------------------------------------------

    def read_ledger(name: str) -> str:
        """Read one of the agent's ledgers.

        Ledgers are the agent's persistent notebooks — `USER` (notes
        about the person being helped) and `MEMORY` (long-term
        memorable facts). Their on-disk locations are resolved
        automatically; do not use `read_file` for these.

        Args:
            name: Ledger to read. One of: "USER", "MEMORY".

        Returns:
            The ledger's contents, or an empty string if unwritten.
        """
        key = name.upper()
        if key not in _LEDGERS:
            valid = ", ".join(sorted(_LEDGERS))
            return f"<unknown ledger: {name!r}; valid: {valid}>"
        _seed_if_missing(key)
        target = _ledger_path(key)
        if not target.exists():
            return ""
        return target.read_text()

    def write_ledger(name: str, content: str) -> str:
        """Overwrite one of the agent's ledgers with new content.

        Ledgers are the agent's persistent notebooks — `USER` and
        `MEMORY`. Their on-disk locations are resolved automatically;
        do not use `write_file` for these. Writes overwrite; if you
        only want to add, `read_ledger` first, edit, then
        `write_ledger`.

        Args:
            name: Ledger to write. One of: "USER", "MEMORY".
            content: Full new content of the ledger.
        """
        key = name.upper()
        if key not in _LEDGERS:
            valid = ", ".join(sorted(_LEDGERS))
            return f"<unknown ledger: {name!r}; valid: {valid}>"
        target = _ledger_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {len(content)} bytes to {target}"

    api.register_tool("read_ledger", read_ledger)
    api.register_tool("write_ledger", write_ledger)

    # ---- Prompt sections --------------------------------------------
    #
    # Two sections, both volatile=False (stable across turns; cache
    # stays warm). The USER section's content changes when the agent
    # writes to it — that breaks the cache for one turn, then re-warms.

    prompt_path = seeds / "PROMPT.md"

    def render_memory_guidance(ctx) -> str:
        """The 'how to use the ledgers' instructional prose."""
        if not prompt_path.exists():
            return ""
        return prompt_path.read_text()

    def render_user_ledger(ctx) -> str:
        """Auto-load USER content into every prompt. Preserves
        pre-plugin behavior where USER.md was splatted into the
        system prompt by SystemPromptBuilder."""
        _seed_if_missing("USER")
        target = _ledger_path("USER")
        if not target.exists():
            return ""
        return target.read_text()

    api.register_prompt_section(
        "memory-guidance", render_memory_guidance, volatile=False
    )
    api.register_prompt_section(
        "user-ledger", render_user_ledger, volatile=False
    )

    # ---- Lifecycle hooks --------------------------------------------

    def on_start(session):
        # Seed both ledgers so the first read returns the template
        # rather than an empty string. Idempotent.
        for name in _LEDGERS:
            _seed_if_missing(name)

        # One-time orphan notice. Users coming from the pre-plugin
        # era have memory at <config-dir>/MEMORY.md and
        # <config-dir>/USER.md. The plugin's storage is at
        # <config-dir>/plugins/memory-markdown/, so legacy files now
        # sit on disk unused. We don't touch user data — just point
        # them out once so the user knows they can delete by hand.
        sentinel = storage / ".legacy-notice-shown"
        if not sentinel.exists():
            legacy = []
            for ledger_name in _LEDGERS.values():
                p = api.config_dir / ledger_name
                if p.exists():
                    legacy.append(str(p))
            if legacy:
                api.log(
                    "info",
                    "memory-markdown: legacy ledger files at "
                    f"{', '.join(legacy)} are no longer used. "
                    "Delete them manually if you wish.",
                )
            sentinel.touch()

    api.on_session_start(on_start)
