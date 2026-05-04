"""memory — bundled ledger + semantic recall.

Backs USER and MEMORY persistence with markdown files plus a
fastembed-backed vector index for `recall_memory`.

USER ledger  — splatted: auto-loaded into every system prompt
                (small, always-relevant: preferences, conventions,
                name, timezone). One file.

MEMORY       — index + per-memory files. MEMORY.md is the catalog
                (auto-loaded into every prompt); each memory is its
                own markdown file under memories/ in the plugin's
                data dir. Agent reads the catalog in the prompt,
                fetches a body with read_memory(file="foo.md") only
                when it needs it. Bodies carry a `created_at` YAML
                frontmatter that the read tools strip on the way out.

Recall: `recall_memory(query)` runs cosine search over an L2-
normalized vector index of hooks + bodies, rebuilt on mtime change.
Index files (vectors.npy, index.json) live alongside MEMORY.md in
the plugin's data dir.

Companion files in this directory:
  manifest.toml
  defaults/MEMORY.md   — seed template for the index file
  defaults/USER.md     — seed template for the per-user notes file
  defaults/PROMPT.md   — pure tool reference; persona-flavored
                         memory prose lives in SOUL.md
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import shutil
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

_LEDGERS = {"USER": "USER.md", "MEMORY": "MEMORY.md"}
_MEMORIES_DIRNAME = "memories"

# ---- Recall (vector) constants ----------------------------------
#
# Embedding model for `recall_memory`. fastembed downloads it on first
# use (~130 MB once-only). bge-small-en-v1.5 is the current sweet
# spot for English embedding speed × quality at agent-memory scale.
_MODEL_NAME = "BAAI/bge-small-en-v1.5"

# Bullet shape recall_memory parses out of MEMORY.md to map filename
# → (title, hook) at query time. Loose enough for `-`, `*`, `+`
# bullets and the various dash/colon separators we've seen between
# title and hook.
_INDEX_LINE_RE = re.compile(
    r"\s*[-*+]\s*\[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)"
    r"(?:\s*[—\-:]\s*(?P<hook>.+))?\s*$"
)

# Process-local cache so multiple recalls in one session don't
# re-instantiate the embedding model. fastembed itself caches model
# weights on disk, but the Python-side ONNX session has nontrivial
# init cost.
_model = None


def _filename_search_terms(filename: str) -> str:
    """Convert a memory filename into search-friendly tokens.

    ``stack_choices.md`` → ``stack choices``. The ``.md`` suffix and
    ``_``/``-`` separators carry no semantic content; replacing them
    with spaces lets the embedder pick up the descriptive tokens the
    agent chose when naming the file. A query like "stack choices"
    now scores against the filename even when the title and hook
    use different wording.
    """
    stem = filename.removesuffix(".md")
    return stem.replace("_", " ").replace("-", " ").strip()


def _get_model():
    """Lazy-init the fastembed TextEmbedding once per process.

    fastembed isn't imported until the first recall — it pulls in
    onnxruntime + tokenizers (~150 MB on disk) plus a one-time
    ~130 MB model download on first non-empty recall. Other memory
    tools (create_memory, read_memory, update_memory) don't pay this cost.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model

# Memory filenames must be lowercase snake_case with a .md suffix.
# Why this strict: filenames are embedded into recall_memory's
# searchable text via _filename_search_terms (above), so a
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
    # short-circuit, ``create_memory("STYLE")`` against an index that
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

    # Persistent ledger storage: <data-dir>/plugins/memory/.
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

    def update_memory(
        filename: str,
        content: str | None = None,
        description: str | None = None,
        category: str | None = None,
        confirm_new_category: bool = False,
    ) -> str:
        """Update fields of an existing memory.

        Edit body content, the description shown beside the title in
        MEMORY.md, the category the bullet is filed under, or any
        combination — the filename keys the existing memory. At
        least one of `content`, `description`, or `category` must
        be set.

        Body writes preserve the existing `created_at` frontmatter
        when the new content lacks one (agents revising a body
        rarely re-emit the YAML). Index updates are atomic via
        temp-then-rename. Body and index writes happen in sequence;
        a crash between them leaves the body in its newer state and
        the index in its prior state, recoverable via a follow-up
        `update_memory` call.

        Drift guard fires on `category`: a close-but-not-equal new
        heading is refused with a marker pointing at the existing
        match. Pass `confirm_new_category=True` to acknowledge that
        the new heading is deliberate.

        Args:
            filename: Bare memory filename whose entry to update.
            content: New body markdown (full overwrite). Existing
                `created_at` frontmatter preserved when absent.
            description: New text after the title in the index
                bullet. Empty string clears it.
            category: New `## <heading>` for the bullet. The bullet
                is moved (not cloned) from its current section.
            confirm_new_category: Acknowledge a category that's
                close to an existing one. Required to bypass the
                drift guard.

        Returns:
            Confirmation listing what changed, or `<...>` error.
        """
        err = _validate_memory_filename(filename)
        if err:
            return err
        if content is None and description is None and category is None:
            return (
                "<update_memory needs at least one of `content`, "
                "`description`, or `category` to be set>"
            )
        if description is not None:
            err = _validate_inline_field("description", description)
            if err:
                return err
        if category is not None:
            if not category.strip():
                return "<category is empty>"
            err = _validate_category(category)
            if err:
                return err

        body_path = _memory_file_path(filename)
        index_path = _ledger_path("MEMORY")
        if not index_path.exists():
            return "<MEMORY.md not found>"
        index_text = index_path.read_text()

        needle = f"]({filename})"
        bullet_present = needle in index_text
        if (description is not None or category is not None) and not bullet_present:
            return f"<no bullet for {filename!r} in MEMORY.md>"

        if category is not None and not confirm_new_category:
            existing_cats = _extract_categories(index_text)
            similar = _find_similar_category(category, existing_cats)
            if similar is not None:
                return (
                    f"<category {category!r} is close to existing "
                    f"category {similar!r} — re-call with "
                    f"category={similar!r} to file under it, or pass "
                    f"confirm_new_category=True to acknowledge a "
                    f"deliberately new heading>"
                )

        actions: list[str] = []

        # Body update first, since a failed index write afterwards
        # can be retried via update_memory; a failed body write
        # before any index touch leaves the index untouched.
        if content is not None:
            if not body_path.exists():
                return f"<body memories/{filename} not found>"
            old_meta, _ = _split_frontmatter(body_path.read_text())
            new_meta, new_body = _split_frontmatter(content)
            if not new_meta and old_meta:
                if not new_body.endswith("\n"):
                    new_body = new_body + "\n"
                final = _format_frontmatter(old_meta) + new_body
            else:
                final = (
                    content if content.endswith("\n") else content + "\n"
                )
            body_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(body_path, final)
            actions.append("body")

        # Index update: rewrite the bullet's description (in place)
        # then relocate (between sections) if both fields set. Order
        # matters because relocation moves the bullet line as a
        # whole — including any description we just spliced into it.
        if description is not None or category is not None:
            new_index = index_text
            if description is not None:
                rewritten: list[str] = []
                for line in new_index.splitlines():
                    idx = line.find(needle)
                    if idx == -1:
                        rewritten.append(line)
                        continue
                    prefix = line[: idx + len(needle)]
                    tail = (
                        f" — {description.strip()}"
                        if description and description.strip()
                        else ""
                    )
                    rewritten.append(prefix + tail)
                new_index = "\n".join(rewritten)
                if not new_index.endswith("\n"):
                    new_index += "\n"
                actions.append("description")

            if category is not None:
                bullet_line: str | None = None
                kept: list[str] = []
                for line in new_index.splitlines():
                    if bullet_line is None and needle in line:
                        bullet_line = line
                        continue
                    kept.append(line)
                if bullet_line is None:
                    return f"<no bullet for {filename!r} in MEMORY.md>"
                rebuilt = "\n".join(kept)
                new_index = _insert_index_bullet(
                    rebuilt, category.strip(), bullet_line
                )
                actions.append(f"category → '{category.strip()}'")

            _atomic_write(index_path, new_index)

        return f"updated {filename}: {', '.join(actions)}"

    def create_memory(
        category: str,
        title: str,
        content: str,
        filename: str = "",
        description: str = "",
        confirm_new_category: bool = False,
    ) -> str:
        """Create a new memory in one call — body file plus index
        entry.

        Writes `memories/<filename>` with `content` (plus a
        `created_at` frontmatter the read tools strip on the way
        out), then inserts a bullet under `## <category>` in
        MEMORY.md. Use for *new* memories; `update_memory` edits an
        existing one, `delete_memory` removes one (curator role
        only).

        Drift guard: a close-but-not-equal `category` is refused
        with a marker pointing at the existing heading. Re-call with
        that heading, or pass `confirm_new_category=True` to
        acknowledge a deliberately new one. Existing categories
        appear in the MEMORY.md section of your prompt.

        Args:
            category: H2 section ("Database", "Style", "Gotchas",
                etc). Matched case-insensitively against existing
                headings; close-but-not-equal matches refused unless
                `confirm_new_category=True`.
            title: Short topical name; the link text in the index.
            content: Full body markdown for the new memory.
            filename: Bare filename under `memories/`, lowercase
                snake_case ASCII ending in `.md`. Empty → derived
                from `title` (`"Stack choices"` → `stack_choices.md`).
            description: One-line text shown after the title in the
                index. What future-you reads to decide whether to
                fetch the body. Empty allowed but costs recall
                accuracy.
            confirm_new_category: Acknowledge a category that's
                close to an existing one. Required to bypass the
                drift guard.

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
        err = _validate_inline_field("description", description)
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
        if not confirm_new_category:
            existing_cats = _extract_categories(index_text)
            similar = _find_similar_category(category, existing_cats)
            if similar is not None:
                return (
                    f"<category {category!r} is close to existing "
                    f"category {similar!r} — re-call with "
                    f"category={similar!r} to file under it, or pass "
                    f"confirm_new_category=True to acknowledge a "
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
        # or the agent can re-link via update_memory).
        bullet = (
            f"- [{title.strip()}]({filename})"
            + (
                f" — {description.strip()}"
                if description and description.strip()
                else ""
            )
        )
        new_index = _insert_index_bullet(
            index_text, category.strip(), bullet
        )
        index_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(index_path, new_index)
        return f"saved {filename} under '{category.strip()}'"

    # ---- Recall (vector) -------------------------------------------
    #
    # Fastembed is a hard dep in pyproject.toml. If it's missing the
    # install is broken — log a clear note and skip just the recall
    # tool rather than failing the whole plugin so add/read/write
    # still work. The plugin's [provides] manifest still declares
    # recall_memory, which means a missing-fastembed install fails
    # `_validate_provides` and the loader rejects the plugin entirely.
    # That's intentional: recall is part of the memory contract and
    # half-loading it silently is worse than refusing.
    try:
        import fastembed  # noqa: F401
        import numpy as np
    except ImportError as exc:
        api.log(
            "warning",
            f"memory: required dependency missing ({exc.name}); "
            "reinstall pyagent. Plugin disabled.",
        )
        return

    def _vec_index_paths() -> tuple[Path, Path]:
        return storage / "vectors.npy", storage / "index.json"

    def _parse_index_entries() -> list[tuple[str, str, str, str]]:
        """Walk MEMORY.md; return (category, title, filename, hook)
        tuples. category is the most recent ## heading above each
        bullet, or "" if the bullet appears before any heading.
        hook may be empty."""
        index_path = _ledger_path("MEMORY")
        if not index_path.exists():
            return []
        out: list[tuple[str, str, str, str]] = []
        current_category = ""
        for line in index_path.read_text().splitlines():
            stripped = line.lstrip()
            if stripped.startswith("## "):
                current_category = stripped[3:].strip()
                continue
            m = _INDEX_LINE_RE.match(line)
            if not m:
                continue
            out.append((
                current_category,
                m.group("title").strip(),
                m.group("file").strip(),
                (m.group("hook") or "").strip(),
            ))
        return out

    def _gather_chunks() -> list[dict]:
        """Walk MEMORY.md + memories/*.md; return list of chunks
        ready to embed. Each chunk is {kind, filename, text}.

        Filename tokens are prepended to both hook and body chunks so
        descriptive filenames the agent chose ("stack_choices.md" →
        "stack choices") contribute to recall match — searches for
        the filename's words now hit even when the title and hook
        use different wording. Frontmatter is stripped from bodies
        before embedding so created_at tokens don't dilute topical
        signal.
        """
        chunks: list[dict] = []
        for _category, title, filename, hook in _parse_index_entries():
            fn_terms = _filename_search_terms(filename)
            text = (
                f"{fn_terms} {title}: {hook}"
                if hook
                else f"{fn_terms} {title}"
            )
            chunks.append({"kind": "hook", "filename": filename, "text": text})
        memories_dir = storage / _MEMORIES_DIRNAME
        if memories_dir.exists():
            for body_path in sorted(memories_dir.glob("*.md")):
                fn_terms = _filename_search_terms(body_path.name)
                _meta, body = _split_frontmatter(body_path.read_text())
                # Filename tokens prepended on their own line so the
                # body text is preserved as-is for the embedder; the
                # double newline keeps them as a "topic anchor"
                # rather than fusing with the body's first sentence.
                chunks.append({
                    "kind": "body",
                    "filename": body_path.name,
                    "text": f"{fn_terms}\n\n{body}",
                })
        return chunks

    def _is_index_stale() -> bool:
        vec_path, idx_path = _vec_index_paths()
        if not vec_path.exists() or not idx_path.exists():
            return True
        idx_mtime = min(
            vec_path.stat().st_mtime, idx_path.stat().st_mtime
        )
        index_path = _ledger_path("MEMORY")
        if (
            index_path.exists()
            and index_path.stat().st_mtime > idx_mtime
        ):
            return True
        memories_dir = storage / _MEMORIES_DIRNAME
        if memories_dir.exists():
            for f in memories_dir.glob("*.md"):
                if f.stat().st_mtime > idx_mtime:
                    return True
        return False

    def _build_and_save():
        chunks = _gather_chunks()
        vec_path, idx_path = _vec_index_paths()
        vec_path.parent.mkdir(parents=True, exist_ok=True)
        if not chunks:
            # Wipe stale on-disk artifacts so an empty store doesn't
            # serve old hits.
            for p in (vec_path, idx_path):
                if p.exists():
                    p.unlink()
            return None, []
        model = _get_model()
        texts = [c["text"] for c in chunks]
        vectors = np.asarray(list(model.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.maximum(norms, 1e-9)
        np.save(vec_path, vectors)
        # Strip the embedded text before saving — we re-derive
        # snippets from source files at query time, no need to
        # store twice.
        meta = [{"kind": c["kind"], "filename": c["filename"]} for c in chunks]
        idx_path.write_text(json.dumps(meta))
        return vectors, meta

    def _load_or_build():
        vec_path, idx_path = _vec_index_paths()
        if _is_index_stale():
            return _build_and_save()
        try:
            vectors = np.load(vec_path)
            meta = json.loads(idx_path.read_text())
            return vectors, meta
        except Exception as exc:
            logger.warning(
                "memory: failed to load saved vector index (%s); "
                "rebuilding",
                exc,
            )
            return _build_and_save()

    def _snippet_for(meta: dict, hook_lookup: dict[str, str]) -> str:
        """Return a human-readable snippet for a hit. For hook hits,
        the hook line; for body hits, the first non-blank lines of
        the body (frontmatter stripped)."""
        if meta["kind"] == "hook":
            return hook_lookup.get(meta["filename"], "")
        body_path = storage / _MEMORIES_DIRNAME / meta["filename"]
        if not body_path.exists():
            return ""
        _m, body = _split_frontmatter(body_path.read_text())
        lines = [ln for ln in body.splitlines() if ln.strip()]
        return "\n".join(lines[:3])

    def recall_memory(
        query: str,
        k: int = 5,
        min_score: float = 0.0,
        category: str | None = None,
    ) -> str:
        """Semantic search over the memory ledger.

        Use when scanning the MEMORY.md index in your prompt isn't
        enough — long catalog, cross-cutting topic, or you remember
        the gist of what you wrote down but not the title or
        filename. Each hit names a file under `memories/` and a
        short snippet; fetch the full body with
        `read_memory(file=<filename>)` only when you need it.

        Args:
            query: What you're looking for, in natural language.
            k: Maximum number of files in the result. Default 5.
            min_score: Drop hits below this cosine similarity.
                Useful range 0.2–0.5; defaults to 0.0 (no threshold).
            category: Restrict to memories filed under this H2
                heading in MEMORY.md (case-insensitive). Use when
                you already know the topic area; avoids dilution
                from off-topic memories sharing keywords.

        Returns:
            A formatted list of hits, or a `<...>` error string.
        """
        if not query or not query.strip():
            return "<empty query>"
        if k < 1:
            return "<k must be >= 1>"
        vectors, meta = _load_or_build()
        if vectors is None or len(meta) == 0:
            return "<no memories indexed yet>"
        model = _get_model()
        q_vec = np.asarray(
            next(iter(model.embed([query]))), dtype=np.float32
        )
        q_vec = q_vec / max(float(np.linalg.norm(q_vec)), 1e-9)
        scores = vectors @ q_vec  # cosine since normalized
        entries = _parse_index_entries()
        file_to_cat = {f: c for c, _t, f, _h in entries}
        hook_lookup = {f: h for _c, _t, f, h in entries if h}
        # Group by filename: keep highest-scoring chunk per file so a
        # body and its hook don't both show up.
        best: dict[str, tuple[float, dict]] = {}
        for i, m in enumerate(meta):
            score = float(scores[i])
            existing = best.get(m["filename"])
            if existing is None or score > existing[0]:
                best[m["filename"]] = (score, m)

        target_cat = category.strip().lower() if category else None
        filtered: list[tuple[str, tuple[float, dict]]] = []
        for filename, (score, m) in best.items():
            if score < min_score:
                continue
            if target_cat is not None:
                actual = file_to_cat.get(filename, "")
                if actual.lower() != target_cat:
                    continue
            filtered.append((filename, (score, m)))
        ranked = sorted(filtered, key=lambda kv: -kv[1][0])[:k]

        # Header reflects active filters so the agent knows what
        # was applied; unfiltered output is unchanged.
        filter_parts = []
        if min_score > 0:
            filter_parts.append(f"min_score={min_score:.2f}")
        if category:
            filter_parts.append(f"category={category!r}")
        filter_suffix = (
            f" ({', '.join(filter_parts)})" if filter_parts else ""
        )

        if not ranked:
            if filter_parts:
                return (
                    f"<no matches for {query!r} with "
                    f"{', '.join(filter_parts)}; "
                    "try a broader filter or drop the threshold>"
                )
            return f"<no matches for {query!r}>"

        lines = [
            f"Top {len(ranked)} matches for {query!r}{filter_suffix}:"
        ]
        for filename, (score, m) in ranked:
            lines.append(
                f"  • memories/{filename}  "
                f"(score={score:.3f}, via {m['kind']})"
            )
            snippet = _snippet_for(m, hook_lookup)
            for s_line in snippet.splitlines()[:3]:
                lines.append(f"      {s_line}")
        lines.append("")
        lines.append('Fetch a body with: read_memory(file="<filename>")')
        return "\n".join(lines)

    def delete_memory(filename: str) -> str:
        """Delete a memory: bullet (if present) AND body file (if
        present). Tolerates orphan state — useful when sweeping a
        catalog that's drifted out of sync with disk, or when
        finishing a rename / split that left one side stranded.

        Refuses only if neither the bullet nor the body exists, so
        the curator gets clear feedback for "nothing to delete here."

        Args:
            filename: Bare memory filename to remove.

        Returns:
            Confirmation naming what was actually removed
            (bullet, body, or both), or `<...>` error.
        """
        err = _validate_memory_filename(filename)
        if err:
            return err

        index_path = _ledger_path("MEMORY")
        body_path = _memory_file_path(filename)

        # Strip the bullet line (if present) from MEMORY.md.
        bullet_removed = False
        if index_path.exists():
            text = index_path.read_text()
            needle = f"]({filename})"
            kept_lines = []
            for line in text.splitlines():
                if not bullet_removed and needle in line:
                    bullet_removed = True
                    continue
                kept_lines.append(line)
            if bullet_removed:
                new_text = "\n".join(kept_lines)
                if not new_text.endswith("\n"):
                    new_text += "\n"
                _atomic_write(index_path, new_text)

        body_removed = False
        if body_path.exists():
            body_path.unlink()
            body_removed = True

        if not bullet_removed and not body_removed:
            return (
                f"<nothing to delete for {filename!r}: no bullet in "
                f"MEMORY.md, no body file on disk>"
            )

        parts: list[str] = []
        if bullet_removed:
            parts.append("bullet from MEMORY.md")
        if body_removed:
            parts.append(f"memories/{filename}")
        return f"deleted {' and '.join(parts)}"

    api.register_tool("create_memory", create_memory)
    api.register_tool("read_memory", read_memory)
    api.register_tool("update_memory", update_memory)
    api.register_tool("delete_memory", delete_memory, role_only=True)
    api.register_tool("write_user", write_user)
    api.register_tool("recall_memory", recall_memory)

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
        available categories before picking one for ``create_memory``
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
        # <data-dir>/plugins/memory/, so legacy files now sit on
        # disk unused. We don't touch user data — just point them
        # out once so the user knows they can delete by hand.
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
                    "memory: legacy ledger files at "
                    f"{', '.join(legacy)} are no longer used. "
                    "Delete them manually if you wish.",
                )
            sentinel.touch()

    api.on_session_start(on_start)
