"""memory-markdown — bundled markdown ledger backend.

USER ledger  — splatted: auto-loaded into every system prompt
                (small, always-relevant: preferences, conventions,
                name, timezone). One file.

MEMORY       — index + per-memory files. MEMORY.md is the catalog
                (auto-loaded into every prompt); each memory is its
                own markdown file under memories/ in the plugin's
                data dir. Agent reads the catalog in the prompt,
                fetches a specific file with read_ledger("MEMORY",
                file="foo.md") only when it needs the body.

Companion files in this directory:
  manifest.toml
  defaults/MEMORY.md   — seed template for the index file
  defaults/USER.md     — seed template for the per-user notes file
  defaults/PROMPT.md   — the "how to use the ledgers" instructional
                         prose, lifted from SOUL.md
"""

from __future__ import annotations

import shutil
from pathlib import Path

_LEDGERS = {"USER": "USER.md", "MEMORY": "MEMORY.md"}
_MEMORIES_DIRNAME = "memories"


def _validate_memory_filename(file: str) -> str | None:
    """Return None if `file` is a safe bare memory filename, else an
    error string suitable to return to the LLM. Rejects path traversal,
    absolute paths, hidden files, and non-`.md` extensions."""
    if not file:
        return "<memory filename is empty>"
    p = Path(file)
    if p.is_absolute() or len(p.parts) != 1:
        return f"<memory filename must be a bare name (no slashes): {file!r}>"
    if file.startswith(".") or ".." in file:
        return f"<invalid memory filename: {file!r}>"
    if not file.endswith(".md"):
        return f"<memory filename must end with .md: {file!r}>"
    return None


def register(api):
    """Plugin entrypoint."""

    plugin_dir = Path(__file__).parent
    seeds = plugin_dir / "defaults"

    # Persistent ledger storage: <data-dir>/plugins/memory-markdown/.
    # Lazy-created on first access.
    storage = api.user_data_dir

    def _ledger_path(name: str) -> Path:
        return storage / _LEDGERS[name]

    def _memory_file_path(file: str) -> Path:
        return storage / _MEMORIES_DIRNAME / file

    def _seed_if_missing(name: str) -> None:
        target = _ledger_path(name)
        if target.exists():
            return
        bundled = seeds / _LEDGERS[name]
        if bundled.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(bundled, target)

    # ---- Tools ------------------------------------------------------

    def read_ledger(name: str, file: str | None = None) -> str:
        """Read one of the agent's ledgers, or a specific memory file.

        Ledgers are the agent's persistent notebooks. `USER` is a
        single-file ledger (notes about the person being helped).
        `MEMORY` is an index + per-memory files: MEMORY.md is the
        catalog, each memory is its own file under `memories/`.

        Args:
            name: Ledger to read. One of: "USER", "MEMORY".
            file: For MEMORY only — name of a specific memory file
                under `memories/` (e.g. "stack_choices.md"). Omit to
                read the MEMORY.md index. Not supported for USER.

        Returns:
            File contents, or an empty string if unwritten. Returns
            an error string in `<...>` form for invalid inputs.
        """
        key = name.upper()
        if key not in _LEDGERS:
            valid = ", ".join(sorted(_LEDGERS))
            return f"<unknown ledger: {name!r}; valid: {valid}>"
        if file is not None:
            if key == "USER":
                return "<USER is a single-file ledger; file argument not supported>"
            err = _validate_memory_filename(file)
            if err:
                return err
            target = _memory_file_path(file)
            if not target.exists():
                return f"<memory not found: memories/{file}>"
            return target.read_text()
        _seed_if_missing(key)
        target = _ledger_path(key)
        if not target.exists():
            return ""
        return target.read_text()

    def write_ledger(
        name: str, content: str, file: str | None = None
    ) -> str:
        """Overwrite a ledger or a specific memory file.

        For USER, omit `file` — USER is a single-file ledger.
        For MEMORY, omit `file` to overwrite the MEMORY.md index;
        pass `file="foo.md"` to write `memories/foo.md`. After
        creating a new memory file, also update MEMORY.md to add a
        pointer in the index — agents see only the index by default.

        Args:
            name: Ledger to write. One of: "USER", "MEMORY".
            content: Full new content.
            file: For MEMORY only — name of a memory file under
                `memories/`. Omit to write the MEMORY.md index.
        """
        key = name.upper()
        if key not in _LEDGERS:
            valid = ", ".join(sorted(_LEDGERS))
            return f"<unknown ledger: {name!r}; valid: {valid}>"
        if file is not None:
            if key == "USER":
                return "<USER is a single-file ledger; file argument not supported>"
            err = _validate_memory_filename(file)
            if err:
                return err
            target = _memory_file_path(file)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"Wrote {len(content)} bytes to {target}"
        target = _ledger_path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Wrote {len(content)} bytes to {target}"

    api.register_tool("read_ledger", read_ledger)
    api.register_tool("write_ledger", write_ledger)

    # ---- Prompt sections --------------------------------------------
    #
    # Three sections, all volatile=False (stable across turns; cache
    # stays warm). USER and MEMORY-INDEX content changes when the
    # agent writes to them — that breaks the cache for one turn,
    # then re-warms.

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

    def render_memory_index(ctx) -> str:
        """Auto-load the MEMORY.md index into every prompt so the
        agent always sees the catalog without a tool call. Body
        files under memories/ are fetched on demand via
        read_ledger("MEMORY", file=...)."""
        _seed_if_missing("MEMORY")
        target = _ledger_path("MEMORY")
        if not target.exists():
            return ""
        return target.read_text()

    api.register_prompt_section(
        "memory-guidance", render_memory_guidance, volatile=False
    )
    api.register_prompt_section(
        "user-ledger", render_user_ledger, volatile=False
    )
    api.register_prompt_section(
        "memory-index", render_memory_index, volatile=False
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
