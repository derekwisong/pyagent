"""memory-markdown — bundled markdown ledger backend.

USER ledger  — splatted: auto-loaded into every system prompt
                (small, always-relevant: preferences, conventions,
                name, timezone). One file.

MEMORY       — index + per-memory files. MEMORY.md is the catalog
                (auto-loaded into every prompt); each memory is its
                own markdown file under memories/ in the plugin's
                data dir. Agent reads the catalog in the prompt,
                fetches a body with read_memory(file="foo.md") only
                when it needs it.

Companion files in this directory:
  manifest.toml
  defaults/MEMORY.md   — seed template for the index file
  defaults/USER.md     — seed template for the per-user notes file
  defaults/PROMPT.md   — the "how to use the ledgers" instructional
                         prose, lifted from SOUL.md
"""

from __future__ import annotations

import datetime
import os
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
    if p.is_absolute():
        return f"<memory filename must not be absolute: {file!r}>"
    if len(p.parts) != 1:
        return f"<memory filename must not contain slashes: {file!r}>"
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


# Minimal YAML-flavored frontmatter parser. We only emit ``created_at:
# <iso>`` blocks today; the parser is intentionally narrow — split on
# the first ``:``, no quoting, no nesting, no lists. If the day comes
# we need richer metadata, swap in a real YAML lib here.
_FRONTMATTER_RE = re.compile(r"\A---\n(?P<inner>.*?)\n---\n?", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Pull a leading ``---\\n...\\n---\\n`` block off ``text``.

    Returns ``(metadata_dict, remaining_body)``. Lines without ``:``
    are skipped silently — malformed frontmatter degrades to empty
    metadata rather than poisoning a body read.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group("inner").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def _format_frontmatter(meta: dict[str, str]) -> str:
    """Inverse of ``_split_frontmatter``. Empty dict → empty string."""
    if not meta:
        return ""
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _now_iso() -> str:
    """Current UTC time as RFC 3339 / ISO 8601, second-precision."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` via ``<path>.tmp`` + ``os.replace``.

    The replace is atomic on POSIX, so a crash mid-write leaves either
    the prior file or the new — never a truncated one. Used for
    MEMORY.md and USER.md to prevent index corruption.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _validate_inline_field(name: str, value: str) -> str | None:
    """Reject newlines in fields that flow into a single MEMORY.md line.

    A bullet line is ``- [<title>](<file>) — <hook>`` on one line; a
    newline in any of those fields breaks the line shape and a future
    parse will silently drop the bullet. Used as a unified gate for
    ``title`` and ``hook``.
    """
    if "\n" in value or "\r" in value:
        return f"<{name} contains a newline; not allowed>"
    return None


def _validate_category(value: str) -> str | None:
    """Newline-reject + leading-``#`` reject for ``category``.

    A leading ``#`` would emit ``## # Foo`` (cosmetic) or, worse,
    ``\\n## Bar`` injected via ``category="Foo\\n## Bar"`` would add a
    parallel heading the next call's ``_extract_categories`` treats as
    real. Cheap belt against an LLM-controlled value flowing into
    markdown structure.
    """
    err = _validate_inline_field("category", value)
    if err:
        return err
    if value.lstrip().startswith("#"):
        return "<category cannot start with '#' (would break heading)>"
    return None


def _derive_filename_from_title(title: str) -> str:
    """Snake_case ``title`` into a memory filename.

    Lowercases, replaces non-alphanumeric runs with ``_``, strips
    leading/trailing underscores, suffixes ``.md``. Returns the empty
    string if the title produces nothing (all-punctuation or empty
    after stripping). The downstream filename validator catches that
    case explicitly so the caller knows to pass an explicit filename.
    """
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    if not s or not s[0].isalnum():
        return ""
    return f"{s}.md"


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

    def read_memory(file: str) -> str:
        """Fetch a memory body from `memories/<file>`.

        USER and the MEMORY.md catalog auto-load into your system
        prompt — no tool to re-read them. Use this only when you've
        spotted a memory in the catalog (or via `recall_memory`)
        and want the body.

        Args:
            file: Bare memory filename, e.g. "stack_choices.md".

        Returns:
            Body text. If a `created_at` frontmatter is present, it's
            stripped and a `[created <iso>]` header is prepended so
            the date is visible without exposing YAML. `<...>` error
            for missing or invalid filenames.
        """
        err = _validate_memory_filename(file)
        if err:
            return err
        target = _memory_file_path(file)
        if not target.exists():
            return f"<memory not found: memories/{file}>"
        meta, body = _split_frontmatter(target.read_text())
        if meta.get("created_at"):
            return f"[created {meta['created_at']}]\n\n{body}"
        return body

    def write_user(content: str) -> str:
        """Overwrite the USER ledger.

        USER auto-loads into every system prompt; the next turn
        sees the change. Atomic write — a crash mid-write keeps
        the prior file rather than truncating it.

        Args:
            content: Full new content for USER.
        """
        target = _ledger_path("USER")
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, content)
        return f"updated USER ({len(content)} bytes)"

    def write_memory(file: str, content: str) -> str:
        """Overwrite an existing memory body, or the MEMORY.md catalog.

        For a body, pass the filename. To create a *new* memory
        (body + index entry in one call), use `add_memory`. To
        rewrite the MEMORY.md catalog itself (rare; consolidation),
        pass `file=""`.

        When editing a body, an existing `created_at` frontmatter is
        preserved if `content` doesn't carry one — agents revising
        bodies rarely re-emit the YAML.

        Args:
            file: Bare memory filename under `memories/`, OR empty
                string to overwrite MEMORY.md.
            content: Full new content.
        """
        if not file or not file.strip():
            target = _ledger_path("MEMORY")
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, content)
            return f"updated MEMORY.md ({len(content)} bytes)"
        err = _validate_memory_filename(file)
        if err:
            return err
        target = _memory_file_path(file)

        final = content
        if target.exists():
            old_meta, _ = _split_frontmatter(target.read_text())
            new_meta, new_body = _split_frontmatter(content)
            if not new_meta and old_meta:
                if not new_body.endswith("\n"):
                    new_body = new_body + "\n"
                final = _format_frontmatter(old_meta) + new_body

        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, final)
        return f"updated {file} ({len(final)} bytes)"

    def add_memory(
        category: str,
        title: str,
        content: str,
        filename: str = "",
        hook: str = "",
        force_new_category: bool = False,
    ) -> str:
        """Add a new memory in one call — body file plus index entry.

        Writes `memories/<filename>` with `content` (plus a
        `created_at` frontmatter), then inserts a bullet under
        `## <category>` in MEMORY.md. Use for *new* memories;
        `write_memory` edits existing ones, `update_memory_hook`
        retunes one bullet without rewriting the index.

        Drift guard: a close-but-not-equal `category` is refused
        with a marker pointing at the existing heading. Re-call with
        that heading, or pass `force_new_category=True` to confirm a
        deliberately new one. Existing categories appear in the
        MEMORY.md section of your prompt.

        Args:
            category: H2 section ("Database", "Style", "Gotchas",
                etc). Matched case-insensitively against existing
                headings; close-but-not-equal matches refused unless
                `force_new_category=True`.
            title: Short topical name; the link text in the index.
            content: Full body markdown for the new memory.
            filename: Bare filename under `memories/`, lowercase
                snake_case ASCII ending in `.md`. Empty → derived
                from `title` (`"Stack choices"` → `stack_choices.md`).
            hook: One-line description shown after the title in the
                index. Empty allowed but costs recall accuracy.
            force_new_category: Skip the drift guard and file under
                `category` as a new heading.

        Returns:
            Confirmation, or `<...>` error/warning marker.
        """
        if not category or not category.strip():
            return "<category is empty>"
        if not title or not title.strip():
            return "<title is empty>"
        err = _validate_category(category)
        if err:
            return err
        err = _validate_inline_field("title", title)
        if err:
            return err
        err = _validate_inline_field("hook", hook)
        if err:
            return err

        if not filename or not filename.strip():
            filename = _derive_filename_from_title(title)
            if not filename:
                return (
                    f"<could not derive a filename from title "
                    f"{title!r}; pass an explicit `filename` like "
                    f"'stack_choices.md'>"
                )
        err = _validate_memory_filename(filename)
        if err:
            return err

        body_path = _memory_file_path(filename)
        _seed_if_missing("MEMORY")
        index_path = _ledger_path("MEMORY")
        index_text = (
            index_path.read_text() if index_path.exists() else ""
        )

        # Validation order: filename → collision → drift. Collision
        # is decisive (can't recover); drift is a soft warning the
        # caller can override. Doing collision first saves a wasted
        # retry where the caller fixed drift only to hit a clash.
        if f"]({filename})" in index_text:
            return (
                f"<filename collision: memories/{filename} is "
                f"already in the index; pick a more specific "
                f'filename, or call read_memory("{filename}") to '
                "inspect what is there>"
            )
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

        # Body: prepend frontmatter and ensure trailing newline.
        # `created_at` lets read_memory and recall surface age.
        body_text = content if content.endswith("\n") else content + "\n"
        body_with_meta = (
            _format_frontmatter({"created_at": _now_iso()}) + body_text
        )

        # O_EXCL on the body file: the OS does the existence check
        # atomically, eliminating the race between the index's "is
        # this filename in use?" check and the actual write.
        body_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(body_path, "x", encoding="utf-8") as f:
                f.write(body_with_meta)
        except FileExistsError:
            return (
                f"<filename collision: memories/{filename} appeared "
                "between check and write; pick a different filename>"
            )

        # Index update is atomic via temp-then-rename so a crash
        # leaves either the prior index or the new — never a
        # truncated one. Body is already on disk; if the rename
        # fails, the body is recoverable (recall_memory finds it,
        # or the agent can re-link via update_memory_hook).
        bullet = (
            f"- [{title.strip()}]({filename})"
            + (f" — {hook.strip()}" if hook and hook.strip() else "")
        )
        new_index = _insert_index_bullet(
            index_text, category.strip(), bullet
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(index_path, new_index)
        return f"saved {filename} under '{category.strip()}'"

    def update_memory_hook(filename: str, new_hook: str) -> str:
        """Update the hook line of one bullet in MEMORY.md, in place.

        The hook is the description after the title — what future-you
        reads to decide whether to fetch the body. Use this when a
        hook is failing recall (generic phrasing, missing distinctive
        tokens) — far cheaper than re-emitting the whole index via
        `write_memory`.

        Args:
            filename: Bare memory filename whose bullet to retune.
            new_hook: Replacement hook text. Empty string clears it.

        Returns:
            Confirmation, or `<...>` error if the bullet isn't found.
        """
        err = _validate_memory_filename(filename)
        if err:
            return err
        err = _validate_inline_field("new_hook", new_hook)
        if err:
            return err

        index_path = _ledger_path("MEMORY")
        if not index_path.exists():
            return "<MEMORY.md not found>"
        text = index_path.read_text()

        # Locate the bullet by its `](filename)` link target. We
        # don't reparse the bullet as markdown — we splice the
        # trailing portion (after the closing `)`) with the new
        # hook. Preserves any indentation, bullet style, or list
        # depth the existing line carries.
        needle = f"]({filename})"
        new_lines: list[str] = []
        found = False
        for line in text.splitlines():
            idx = line.find(needle)
            if idx == -1:
                new_lines.append(line)
                continue
            prefix = line[: idx + len(needle)]
            tail = (
                f" — {new_hook.strip()}"
                if new_hook and new_hook.strip()
                else ""
            )
            new_lines.append(prefix + tail)
            found = True
        if not found:
            return f"<no bullet for {filename!r} in MEMORY.md>"

        new_text = "\n".join(new_lines)
        if not new_text.endswith("\n"):
            new_text += "\n"
        _atomic_write(index_path, new_text)
        return f"updated hook for {filename}"

    api.register_tool("read_memory", read_memory)
    api.register_tool("write_user", write_user)
    api.register_tool("write_memory", write_memory)
    api.register_tool("add_memory", add_memory)
    api.register_tool("update_memory_hook", update_memory_hook)

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
        read_memory(file=...).

        When the index has many headings, prepend a one-line
        ``Categories in use: ...`` summary so the agent can scan
        available categories before picking one for ``add_memory``
        without parsing the full bulleted detail. The summary is
        synthesized at render time only — the source MEMORY.md
        file stays clean so ``write_memory`` round-trips don't
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
