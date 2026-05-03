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

import re
import shutil
from difflib import SequenceMatcher
from pathlib import Path

_LEDGERS = {"USER": "USER.md", "MEMORY": "MEMORY.md"}
_MEMORIES_DIRNAME = "memories"

# Memory filenames must be lowercase snake_case with a .md suffix.
# Why this strict: filenames are now embedded into recall_memory's
# searchable text (memory_vector._filename_search_terms), so a
# consistent shape keeps recall predictable. Also stops the agent
# from drifting into mixed-case or spaced filenames that look
# inconsistent in the index.
_FILENAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*\.md$")

# Two-stage drift check:
#
# (1) Token-level containment. Splitting on whitespace, if the
#     target's token set is a strict subset/superset of an existing
#     category's, that's the "Code Style" / "Style" case — flag it.
#     Pure character similarity misses this (ratio is only ~0.67),
#     and lowering the threshold to catch it produces false positives
#     on unrelated 1-char-different names ("Stack" vs "Slack").
#
# (2) Fuzzy similarity at 0.85 for the leftover cases — pluralization
#     ("Database" vs "Databases" ~0.94, "Style" vs "Styles" ~0.91)
#     and minor typos. 0.85 is high enough to ignore "Stack" vs
#     "Slack" (0.80) and "Stack" vs "Snack" (0.80).
_CATEGORY_FUZZY_THRESHOLD = 0.85

# When the rendered MEMORY.md section has at least this many
# categories, we prepend a compact "Categories in use: ..." summary
# above the bulleted detail so the agent can scan available
# headings without parsing the full structure. Below this count
# the bullets are short enough that scanning them is cheap.
_CATEGORY_SUMMARY_MIN = 5


def _extract_categories(index_text: str) -> list[str]:
    """Walk ``MEMORY.md``-style text and return its ``## <heading>``
    names in document order, deduplicated case-insensitively (the
    insert path matches headings case-insensitively so two ``##``
    lines that differ only in case shouldn't both surface)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in index_text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("## "):
            continue
        name = stripped[3:].strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _find_similar_category(
    target: str, existing: list[str]
) -> str | None:
    """Return the closest existing category name if it's confusingly
    close to ``target``, else None. Case-insensitive comparison.

    Two-stage check (see comment block on the threshold constant for
    why):

      1. Token-level subset/superset — catches compound-vs-bare
         names like ``Code Style`` vs ``Style``. Pure character
         similarity misses this case.
      2. Fuzzy similarity ≥ 0.85 — catches pluralization
         (``Database`` vs ``Databases``, ``Decision`` vs
         ``Decisions``) without flagging unrelated short names that
         differ by one character (``Stack`` vs ``Slack``).

    Exact case-insensitive matches return None — those collapse via
    ``_insert_index_bullet``'s existing case-insensitive matching, no
    warning needed.
    """
    target_lc = target.lower().strip()
    if not target_lc:
        return None

    # If ANY existing category matches case-insensitively, defer to
    # _insert_index_bullet's case-insensitive collapse. Without this
    # short-circuit, ``add_memory("STYLE")`` against an index that
    # already has both ``Style`` and ``Code Style`` would trip the
    # subset check on ``Code Style`` and refuse — but the canonical
    # destination is the existing literal-match ``Style``.
    for cat in existing:
        if cat.lower().strip() == target_lc:
            return None

    target_tokens = set(target_lc.split())

    # Stage 1: token containment (strict subset / superset).
    for cat in existing:
        cat_lc = cat.lower().strip()
        if not cat_lc:
            continue
        cat_tokens = set(cat_lc.split())
        if not target_tokens or not cat_tokens:
            continue
        if target_tokens < cat_tokens or cat_tokens < target_tokens:
            return cat

    # Stage 2: fuzzy similarity for the leftover near-equal cases.
    best_ratio = 0.0
    best_match: str | None = None
    for cat in existing:
        cat_lc = cat.lower().strip()
        if not cat_lc:
            continue
        r = SequenceMatcher(None, target_lc, cat_lc).ratio()
        if r > best_ratio:
            best_ratio = r
            best_match = cat
    if best_ratio >= _CATEGORY_FUZZY_THRESHOLD:
        return best_match
    return None


def _validate_memory_filename(file: str) -> str | None:
    """Return None if `file` is a safe bare memory filename, else an
    error string suitable to return to the LLM. Rejects path traversal,
    absolute paths, hidden files, non-`.md` extensions, and anything
    that isn't lowercase snake_case + ASCII (filenames feed into
    recall search; convention keeps results predictable)."""
    if not file:
        return "<memory filename is empty>"
    p = Path(file)
    if p.is_absolute() or len(p.parts) != 1:
        return f"<memory filename must be a bare name (no slashes): {file!r}>"
    if file.startswith(".") or ".." in file:
        return f"<invalid memory filename: {file!r}>"
    if not file.endswith(".md"):
        return f"<memory filename must end with .md: {file!r}>"
    if not _FILENAME_RE.match(file):
        return (
            f"<memory filename must be lowercase snake_case ASCII "
            f"(matching {_FILENAME_RE.pattern!r}): {file!r}; "
            f"e.g. 'stack_choices.md', 'client_naming_convention.md'>"
        )
    return None


def _insert_index_bullet(
    index_text: str, category: str, bullet: str
) -> str:
    """Insert `bullet` under `## <category>` in `index_text`.

    Match category case-insensitively against existing H2 headings.
    If the heading is absent, append a new `## <category>` section
    at the end of the file. Strips the `(no memories yet)` seed
    placeholder if present. Returns the new full text.
    """
    lines = [
        ln for ln in index_text.splitlines()
        if ln.strip() != "(no memories yet)"
    ]

    target_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("## "):
            heading = stripped[3:].strip()
            if heading.lower() == category.lower():
                target_idx = i
                break

    if target_idx is not None:
        # Find end of this section: next H2, or EOF.
        insert_at = len(lines)
        for j in range(target_idx + 1, len(lines)):
            if lines[j].lstrip().startswith("## "):
                insert_at = j
                break
        # Step back past trailing blanks so bullets cluster directly
        # under the heading.
        while (
            insert_at > target_idx + 1
            and lines[insert_at - 1].strip() == ""
        ):
            insert_at -= 1
        lines.insert(insert_at, bullet)
    else:
        # New category goes at the end.
        while lines and lines[-1].strip() == "":
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f"## {category}")
        lines.append(bullet)

    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    return text


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
        """Read a ledger or a specific memory file.

        Ledgers are the agent's persistent notebooks. `USER` is a
        single-file ledger (notes about the person being helped).
        `MEMORY` is an index + per-memory files: MEMORY.md is the
        catalog (auto-loaded into your prompt), each memory is its
        own file under `memories/` (loaded only on demand).

        Primary use: fetching a *known* memory body once you've
        identified it — by scanning the index in your prompt or via
        `recall_memory(query)` if it's available. Reading the MEMORY.md
        index directly is rarely needed since it's already in the
        prompt.

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
        """Overwrite a ledger or a specific memory file (in-place
        edits and consolidation).

        For *new* memories, prefer `add_memory(...)` — it writes
        the body and updates the index in one call. write_ledger is
        for editing what's already there: revising a body, pruning
        an entry, merging fragmentary memories, moving one to a
        different category, or sweeping the catalog. It's also how
        you write USER, which is a single-file ledger and has no
        add_memory equivalent.

        For USER, omit `file` — USER is a single-file ledger.
        For MEMORY, omit `file` to overwrite the MEMORY.md index
        directly (e.g. when reorganizing); pass `file="foo.md"` to
        overwrite `memories/foo.md`.

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

    def add_memory(
        category: str,
        title: str,
        filename: str,
        hook: str,
        content: str,
        force_new_category: bool = False,
    ) -> str:
        """Add a new memory in one call — body file plus index entry.

        Writes `memories/<filename>` with `content`, then surgically
        inserts a bullet line under `## <category>` in MEMORY.md
        (creating the heading if absent, case-insensitive match for
        existing). Saves the round-trip cost of re-emitting the
        full index every time you save a memory.

        Use this for *new* memories. To update an existing body in
        place, use `write_ledger("MEMORY", content, file=...)`. To
        prune, edit MEMORY.md and delete the body file directly.

        Category drift guard: if `category` is close (but not equal)
        to an existing heading in MEMORY.md, this call refuses with
        a marker pointing at the closer match. Re-call with the
        existing category to file under it, or pass
        `force_new_category=True` to confirm a deliberately new
        heading. The guard prevents drift toward parallel headings
        (`Style` + `Code Style` + `Conventions` for one concept).

        Args:
            category: H2 section to file under (e.g. "Database",
                "Style", "Gotchas"). Matched case-insensitively
                against existing headings; new heading created at
                the end of the index if no match. Close-but-not-
                equal matches against existing headings are
                refused unless `force_new_category=True`.
            title: Short topical name used as the link text.
            filename: Bare filename under `memories/`, must end
                in `.md`. Cannot collide with an existing file.
            hook: One-line description shown after the title in
                the index — what future-you reads to decide
                whether to fetch. May be empty if the title alone
                is enough.
            content: Full body markdown for the new memory file.
            force_new_category: When True, skip the drift guard and
                file under `category` even if it's close to an
                existing heading. Use when you genuinely want a new
                heading near an existing one (rare).

        Returns:
            A confirmation string with both written paths, or an
            error / drift-warning in `<...>` form.
        """
        if not category or not category.strip():
            return "<category is empty>"
        if not title or not title.strip():
            return "<title is empty>"
        err = _validate_memory_filename(filename)
        if err:
            return err
        body_path = _memory_file_path(filename)
        _seed_if_missing("MEMORY")
        index_path = _ledger_path("MEMORY")
        index_text = (
            index_path.read_text() if index_path.exists() else ""
        )

        # Drift guard: refuse close-but-not-equal category matches.
        # Exact case-insensitive matches fall through to
        # _insert_index_bullet which collapses them.
        if not force_new_category:
            existing_cats = _extract_categories(index_text)
            similar = _find_similar_category(category, existing_cats)
            if similar is not None:
                return (
                    f"<category {category!r} is close to existing "
                    f"category {similar!r} — re-call with "
                    f"category={similar!r} to file under it, or pass "
                    f"force_new_category=True to confirm a "
                    f"deliberately new heading>"
                )
        # Same disambiguation guidance whether the collision is on
        # disk, in the index, or both: pick a different filename, or
        # inspect the existing memory before deciding what to do.
        if body_path.exists() or f"]({filename})" in index_text:
            return (
                f"<filename collision: memories/{filename} is "
                "already taken; pick a more specific filename, "
                f'or call read_ledger("MEMORY", file="{filename}") '
                "to inspect what is already there>"
            )

        # Write the body first — if the index update fails, the
        # body is at least findable via recall_memory and the agent
        # can re-link.
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_text(content)

        bullet = (
            f"- [{title.strip()}]({filename})"
            + (f" — {hook.strip()}" if hook and hook.strip() else "")
        )
        new_index = _insert_index_bullet(
            index_text, category.strip(), bullet
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(new_index)
        return (
            f"Wrote {len(content)} bytes to {body_path}; "
            f"added index entry under '## {category.strip()}'."
        )

    api.register_tool("read_ledger", read_ledger)
    api.register_tool("write_ledger", write_ledger)
    api.register_tool("add_memory", add_memory)

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
        read_ledger("MEMORY", file=...).

        When the index has many headings, prepend a one-line
        ``Categories in use: ...`` summary so the agent can scan
        available categories before picking one for ``add_memory``
        without parsing the full bulleted detail. The summary is
        synthesized at render time only — the source MEMORY.md
        file stays clean so ``write_ledger`` round-trips don't
        fight a derived line.
        """
        _seed_if_missing("MEMORY")
        target = _ledger_path("MEMORY")
        if not target.exists():
            return ""
        text = target.read_text()
        cats = _extract_categories(text)
        if len(cats) >= _CATEGORY_SUMMARY_MIN:
            summary = (
                f"\n*Categories in use: {', '.join(sorted(cats))}.*\n"
            )
            # Insert immediately after the H1 heading line so the
            # summary sits at the top of the section, above the
            # template preamble and the bulleted detail.
            head, sep, tail = text.partition("\n")
            if head.startswith("# ") and sep:
                text = f"{head}{sep}{summary}{tail}"
            else:
                text = f"{summary}\n{text}"
        return text

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
