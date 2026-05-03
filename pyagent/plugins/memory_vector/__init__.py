"""memory-vector — semantic recall over memory-markdown's ledger.

Reads MEMORY.md (the index file) and memories/*.md (bodies) from
memory-markdown's data dir, embeds them with fastembed, and serves
top-K cosine matches for a query string.

Index storage in this plugin's own data dir:
  vectors.npy   numpy array of L2-normalized embeddings, one row per
                indexed chunk
  index.json    parallel list of {"kind": "hook"|"body", "filename":
                "<file>.md"} for each row

Cache invalidation is mtime-based: if any source file (MEMORY.md or
a body in memories/) is newer than the saved index, we rebuild from
scratch. At personal-memory scale (tens to hundreds of files),
rebuild is sub-second after the model is loaded.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from pyagent import paths

logger = logging.getLogger(__name__)

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_INDEX_LINE_RE = re.compile(
    r"\s*[-*+]\s*\[(?P<title>[^\]]+)\]\((?P<file>[^)]+\.md)\)"
    r"(?:\s*[—\-:]\s*(?P<hook>.+))?\s*$"
)


def _filename_search_terms(filename: str) -> str:
    """Convert a memory filename into search-friendly tokens.

    ``stack_choices.md`` → ``stack choices``. The ``.md`` suffix
    and ``_`` / ``-`` separators carry no semantic content;
    replacing them with spaces lets the embedding pick up the
    descriptive tokens the agent chose when naming the file. A
    query like "stack choices" now scores against the filename
    even when the title and hook use different wording.
    """
    stem = filename.removesuffix(".md")
    return stem.replace("_", " ").replace("-", " ").strip()

# Module-level cache so multiple recalls in one session don't reload.
_model = None


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model


def register(api):
    try:
        import fastembed  # noqa: F401
        import numpy as np  # noqa: F401
    except ImportError as exc:
        # fastembed is a hard dep in pyproject.toml — if it's missing
        # the install is broken. Log clearly and skip registration so
        # the loader fails the plugin loud rather than crashing.
        api.log(
            "warning",
            f"memory-vector: required dependency missing ({exc.name}); "
            "reinstall pyagent. Plugin disabled.",
        )
        return

    storage = api.user_data_dir
    source = paths.data_dir() / "plugins" / "memory-markdown"

    def _index_paths() -> tuple[Path, Path]:
        return storage / "vectors.npy", storage / "index.json"

    def _parse_index_entries() -> list[tuple[str, str, str, str]]:
        """Return list of (category, title, filename, hook) tuples
        parsed from MEMORY.md. category is the most recent ## heading
        seen above the bullet, or "" if the bullet appears before any
        heading. hook may be empty if the index entry omits it."""
        index_path = source / "MEMORY.md"
        if not index_path.exists():
            return []
        out = []
        current_category = ""
        for line in index_path.read_text().splitlines():
            stripped = line.lstrip()
            if stripped.startswith("## "):
                current_category = stripped[3:].strip()
                continue
            m = _INDEX_LINE_RE.match(line)
            if not m:
                continue
            out.append(
                (
                    current_category,
                    m.group("title").strip(),
                    m.group("file").strip(),
                    (m.group("hook") or "").strip(),
                )
            )
        return out

    def _gather_chunks() -> list[dict]:
        """Walk MEMORY.md + memories/*.md; return list of chunks
        ready to embed. Each chunk has {kind, filename, text}.

        Filename tokens are prepended to both hook and body chunks
        so descriptive filenames the agent chose (``stack_choices.md``
        → "stack choices") contribute to recall match — searches
        for the filename's words now hit even when the title and
        hook use different wording.
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
        memories_dir = source / "memories"
        if memories_dir.exists():
            for body_path in sorted(memories_dir.glob("*.md")):
                fn_terms = _filename_search_terms(body_path.name)
                # Filename tokens prepended on their own line so the
                # body text is preserved as-is for the embedder; the
                # double newline keeps them as a "topic anchor"
                # rather than fusing with the body's first sentence.
                chunks.append(
                    {
                        "kind": "body",
                        "filename": body_path.name,
                        "text": f"{fn_terms}\n\n{body_path.read_text()}",
                    }
                )
        return chunks

    def _is_index_stale() -> bool:
        vec_path, idx_path = _index_paths()
        if not vec_path.exists() or not idx_path.exists():
            return True
        idx_mtime = min(
            vec_path.stat().st_mtime, idx_path.stat().st_mtime
        )
        index_path = source / "MEMORY.md"
        if (
            index_path.exists()
            and index_path.stat().st_mtime > idx_mtime
        ):
            return True
        memories_dir = source / "memories"
        if memories_dir.exists():
            for f in memories_dir.glob("*.md"):
                if f.stat().st_mtime > idx_mtime:
                    return True
        return False

    def _build_and_save():
        import numpy as np

        chunks = _gather_chunks()
        vec_path, idx_path = _index_paths()
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
        # Strip the embedded text before saving — we re-derive snippets
        # from source files at query time, no need to store twice.
        meta = [{"kind": c["kind"], "filename": c["filename"]} for c in chunks]
        idx_path.write_text(json.dumps(meta))
        return vectors, meta

    def _load_or_build():
        import numpy as np

        vec_path, idx_path = _index_paths()
        if _is_index_stale():
            return _build_and_save()
        try:
            vectors = np.load(vec_path)
            meta = json.loads(idx_path.read_text())
            return vectors, meta
        except Exception as exc:
            logger.warning(
                "memory-vector: failed to load saved index (%s); "
                "rebuilding",
                exc,
            )
            return _build_and_save()

    def _snippet_for(meta: dict, hook_lookup: dict[str, str]) -> str:
        """Return a human-readable snippet for a hit. For hook hits,
        the hook line; for body hits, the first non-blank lines of
        the body."""
        if meta["kind"] == "hook":
            return hook_lookup.get(meta["filename"], "")
        body_path = source / "memories" / meta["filename"]
        if not body_path.exists():
            return ""
        lines = [
            ln for ln in body_path.read_text().splitlines() if ln.strip()
        ]
        return "\n".join(lines[:3])

    def recall_memory(
        query: str,
        k: int = 5,
        min_score: float = 0.0,
        category: str | None = None,
    ) -> str:
        """Semantic search over the memory ledger.

        Returns the top-k memory files matching `query`, scored by
        cosine similarity over both index-hook lines and body
        contents. Use this when scanning the MEMORY.md index in your
        prompt isn't enough — e.g. when you remember the gist of
        what you wrote down but not the title or filename.

        Each hit names a file under `memories/` and a short snippet.
        Fetch the full body with `read_ledger("MEMORY",
        file="<filename>")` only when you need it.

        Args:
            query: What you're looking for, in natural language.
            k: Maximum number of memory files in the result.
                Default 5.
            min_score: Drop hits below this cosine similarity. Use
                to filter low-confidence noise — typical useful
                range is 0.2–0.5; defaults to 0.0 (no threshold).
            category: Restrict to memories filed under this H2
                heading in MEMORY.md (case-insensitive). Use when
                you already know the topic area; avoids dilution
                from off-topic memories that share keywords.
                Defaults to None (search everything).

        Returns:
            A formatted list of hits, or a `<...>` error string.
        """
        import numpy as np

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

        # Header reflects active filters so the agent knows what was
        # applied; unfiltered output is unchanged.
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
        lines.append(
            'Fetch a body with: read_ledger("MEMORY", file="<filename>")'
        )
        return "\n".join(lines)

    api.register_tool("recall_memory", recall_memory)

    # ---- Prompt section --------------------------------------------
    #
    # Spells out the index→read_ledger / fishing→recall_memory
    # decision tree so the agent isn't left to infer it from two
    # disconnected tool docstrings. Skipped entirely when MEMORY.md
    # is empty — there's nothing to recall, and the recall_memory
    # tool's own docstring covers the choice for when the index
    # later fills. Saves ~459 tokens on every cache miss for fresh
    # installs / users who don't actively curate memories.

    def render_guidance(ctx) -> str:
        try:
            if not _parse_index_entries():
                return ""
        except OSError:
            # Filesystem hiccup → render the guidance anyway. Better
            # to keep the prose than to lose it on a transient read
            # error.
            pass
        return (
            "## Recalling memories by similarity\n"
            "\n"
            "When the MEMORY.md index in your prompt has what you "
            'need — you can see the title and hook — call '
            '`read_ledger("MEMORY", file="…")` directly to fetch the '
            "body.\n"
            "\n"
            "Reach for `recall_memory(query)` instead when scanning "
            "the index isn't enough: the catalog is long, the topic "
            "cuts across multiple memories, or you remember the "
            "*gist* of what you wrote down but not the filename. "
            "recall_memory returns the top matching files with "
            "snippets; fetch the ones you want with read_ledger.\n"
            "\n"
            "Rule of thumb: **index → read_ledger** for direct "
            "fetches; **recall_memory** for fishing.\n"
            "\n"
            "Filters when you need them: pass `min_score=0.3` "
            "(or higher) to drop low-confidence noise, and "
            "`category=\"Database\"` to scope to one H2 section "
            "in MEMORY.md when you already know the topic area."
        )

    api.register_prompt_section(
        "memory-vector-guidance", render_guidance, volatile=False
    )
