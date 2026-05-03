"""Filesystem-backed conversation session.

A session lives at <root>/<id>/ and contains:
  - conversation.jsonl: append-only chat history (one JSON object per line)
  - attachments/: large tool outputs offloaded out of the chat log

Tools can return an `Attachment(text=..., preview=...)` to explicitly opt
into offloading; the agent additionally applies a size-based fallback.
"""

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import petname

logger = logging.getLogger(__name__)


@dataclass
class Attachment:
    """A tool return that should be written to the session's attachments dir.

    `content` is the full payload saved to disk — `str` for text
    attachments, `bytes` for binary (e.g. an image, a PDF). `preview`
    is a short summary shown inline in the chat alongside the file
    reference; for text attachments, if empty, the agent falls back to
    truncating `content`. `suffix` is the filename extension to use
    when saving (e.g. `".png"`); defaults pick `.txt` for str, `.bin`
    for bytes.

    `inline_text`, when set, decouples "what the agent sees inline"
    from "what's saved on disk". The agent renders ``inline_text``
    followed by a minimal ``[also saved: <path>]`` footer instead of
    the offload header + preview. Use this when the saved file is
    structured side data (e.g. a JSON blob a downstream tool will
    read) and ``content`` is no longer a candidate for inline preview.
    When ``None`` (default), behavior is unchanged: the saved bytes
    drive the inline preview through the offload header path.
    """

    content: str | bytes
    preview: str = ""
    suffix: str = ""
    inline_text: str | None = None


class Session:
    DEFAULT_ROOT = Path(".pyagent/sessions")
    attachment_threshold = 8000
    preview_chars = 1000
    # Soft cap on total attachments-dir size, in megabytes. After each
    # write, if the dir exceeds this we evict least-recently-accessed
    # files (atime, mtime fallback) until under the cap. The just-
    # written file is always preserved. 0 disables eviction entirely.
    attachment_dir_cap_mb: int = 25

    def __init__(
        self,
        session_id: str | None = None,
        root: Path | None = None,
        attachment_dir_cap_mb: int | None = None,
    ) -> None:
        self.root = root if root is not None else self.DEFAULT_ROOT
        self.id = session_id or self._unique_id(self.root)
        self.dir = self.root / self.id
        self.attachments_dir = self.dir / "attachments"
        self.conversation_path = self.dir / "conversation.jsonl"
        if attachment_dir_cap_mb is not None:
            # Per-instance override of the class-level default. Keeps
            # the class attribute as the single source of truth for the
            # default while letting config wiring inject a different
            # cap without subclassing.
            self.attachment_dir_cap_mb = attachment_dir_cap_mb

    @classmethod
    def list_ids(cls, root: Path | None = None) -> list[str]:
        root = root if root is not None else cls.DEFAULT_ROOT
        if not root.exists():
            return []
        entries = [p for p in root.iterdir() if p.is_dir()]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.name for p in entries]

    @classmethod
    def _unique_id(cls, root: Path) -> str:
        for _ in range(10):
            sid = f"{date.today().isoformat()}-{petname.generate(words=2, separator='-')}"
            if not (root / sid).exists():
                return sid
        raise RuntimeError("could not generate a unique session id after 10 tries")

    def exists(self) -> bool:
        return self.dir.exists()

    def _ensure_dirs(self) -> None:
        self.attachments_dir.mkdir(parents=True, exist_ok=True)

    def load_history(self) -> list[Any]:
        if not self.conversation_path.exists():
            return []
        with self.conversation_path.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    def append_history(self, entries: list[Any]) -> None:
        if not entries:
            return
        self._ensure_dirs()
        with self.conversation_path.open("a") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def write_attachment(
        self,
        tool_name: str,
        content: str | bytes,
        suffix: str = "",
    ) -> Path:
        self._ensure_dirs()
        if not suffix:
            suffix = ".txt" if isinstance(content, str) else ".bin"
        # 8-char uuid suffix is collision-proof across concurrent processes
        # resuming the same session, and the dir listing still groups by tool.
        path = (
            self.attachments_dir
            / f"{tool_name}-{uuid.uuid4().hex[:8]}{suffix}"
        )
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        # After every write, run LRU eviction if the dir is over cap.
        # The just-written `path` is always exempt — even a single
        # write that exceeds the cap by itself stays put (we evict
        # everything older first, then stop). cap=0 disables eviction.
        if self.attachment_dir_cap_mb > 0:
            self._evict_lru_until_under_cap(exclude=path)
        return path

    def _evict_lru_until_under_cap(self, exclude: Path) -> int:
        """Delete least-recently-accessed attachments until under cap.

        Eviction policy:
          - Sort files in `attachments_dir` by access time ascending
            (oldest atime first). Some filesystems mount with
            `noatime` and never update atime; on those, atime equals
            ctime/mtime, so falling back to mtime is implicit when the
            two agree. We still prefer atime explicitly because on a
            filesystem that DOES track it, "haven't read this in a
            while" is the policy we want.
          - The file at `exclude` (the just-written attachment) is
            never evicted, even if removing every other file still
            leaves us over cap. A single huge write that exceeds the
            cap on its own is allowed; the alternative (refusing the
            write or deleting it) breaks forward progress mid-task,
            which is exactly what the LRU design avoids per the issue.
          - Path A re: conversation.jsonl: we do NOT rewrite the JSONL
            when an evicted file was referenced. A future re-read
            attempt by the agent will hit the standard "file not
            found" marker, which is acceptable signal. Documenting
            here so the contract is grep-able alongside the eviction
            logic.

        Returns the count of files evicted.
        """
        cap_bytes = self.attachment_dir_cap_mb * 1024 * 1024
        if cap_bytes <= 0:
            return 0
        if not self.attachments_dir.exists():
            return 0

        # Collect (atime, size, path) for everything in the dir. We
        # gather sizes up front so the running total is stable as we
        # unlink — no re-stat per iteration.
        entries: list[tuple[float, int, Path]] = []
        total = 0
        try:
            exclude_resolved = exclude.resolve()
        except OSError:
            exclude_resolved = exclude
        for child in self.attachments_dir.iterdir():
            if not child.is_file():
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append((st.st_atime, st.st_size, child))
            total += st.st_size

        if total <= cap_bytes:
            return 0

        # Sort oldest-atime first. Tie-break by size descending so a
        # cluster of same-atime files (common on noatime fs) prefers
        # to drop bigger ones first — fewer evictions to get under.
        entries.sort(key=lambda e: (e[0], -e[1]))

        evicted = 0
        for _atime, size, child in entries:
            if total <= cap_bytes:
                break
            try:
                if child.resolve() == exclude_resolved:
                    continue
            except OSError:
                if child == exclude:
                    continue
            try:
                child.unlink()
            except OSError as e:
                logger.warning("attachment eviction skipped %s: %s", child, e)
                continue
            total -= size
            evicted += 1
        return evicted

    def find_orphan_attachments(self) -> list[Path]:
        """Return attachment files not referenced by conversation.jsonl.

        Orphans are typically left behind when a turn errored mid-flight
        (the conversation entry was rolled back, the attachment on disk
        was not). They're harmless but waste space; sweep on resume.
        """
        if not self.attachments_dir.exists() or not self.conversation_path.exists():
            return []
        log_text = self.conversation_path.read_text()
        # Anchor with the "attachments/" segment so a bare filename can't
        # accidentally match unrelated content elsewhere in the log.
        return [
            f
            for f in self.attachments_dir.iterdir()
            if f.is_file() and f"attachments/{f.name}" not in log_text
        ]

    def purge_orphan_attachments(
        self, orphans: list[Path] | None = None
    ) -> int:
        """Delete orphan attachments and return the count removed.

        Pass `orphans` to skip the rescan if the caller already has the
        list (e.g. from `find_orphan_attachments`).
        """
        if orphans is None:
            orphans = self.find_orphan_attachments()
        for f in orphans:
            f.unlink()
        return len(orphans)
