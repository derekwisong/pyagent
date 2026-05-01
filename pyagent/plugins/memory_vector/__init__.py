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

    def _parse_index_hooks() -> list[tuple[str, str, str]]:
        """Return list of (title, filename, hook) tuples parsed from
        MEMORY.md. hook may be empty if the index entry omits it."""
        index_path = source / "MEMORY.md"
        if not index_path.exists():
            return []
        out = []
        for line in index_path.read_text().splitlines():
            m = _INDEX_LINE_RE.match(line)
            if not m:
                continue
            out.append(
                (
                    m.group("title").strip(),
                    m.group("file").strip(),
                    (m.group("hook") or "").strip(),
                )
            )
        return out

    def _gather_chunks() -> list[dict]:
        """Walk MEMORY.md + memories/*.md; return list of chunks
        ready to embed. Each chunk has {kind, filename, text}."""
        chunks: list[dict] = []
        for title, filename, hook in _parse_index_hooks():
            text = f"{title}: {hook}" if hook else title
            chunks.append({"kind": "hook", "filename": filename, "text": text})
        memories_dir = source / "memories"
        if memories_dir.exists():
            for body_path in sorted(memories_dir.glob("*.md")):
                chunks.append(
                    {
                        "kind": "body",
                        "filename": body_path.name,
                        "text": body_path.read_text(),
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

    def recall_memory(query: str, k: int = 5) -> str:
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
            k: How many top hits to return. Default 5.

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
        # Group by filename: keep highest-scoring chunk per file so a
        # body and its hook don't both show up.
        best: dict[str, tuple[float, dict]] = {}
        for i, m in enumerate(meta):
            score = float(scores[i])
            existing = best.get(m["filename"])
            if existing is None or score > existing[0]:
                best[m["filename"]] = (score, m)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:k]
        hook_lookup = {
            f: h for _, f, h in _parse_index_hooks() if h
        }
        lines = [f"Top {len(ranked)} matches for {query!r}:"]
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
    # disconnected tool docstrings.

    def render_guidance(ctx) -> str:
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
            "fetches; **recall_memory** for fishing."
        )

    api.register_prompt_section(
        "memory-vector-guidance", render_guidance, volatile=False
    )
