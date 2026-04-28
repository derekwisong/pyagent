"""Filesystem-backed conversation session.

A session lives at <root>/<id>/ and contains:
  - conversation.jsonl: append-only chat history (one JSON object per line)
  - attachments/: large tool outputs offloaded out of the chat log

Tools can return an `Attachment(text=..., preview=...)` to explicitly opt
into offloading; the agent additionally applies a size-based fallback.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import petname


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
    """

    content: str | bytes
    preview: str = ""
    suffix: str = ""


class Session:
    DEFAULT_ROOT = Path(".pyagent/sessions")
    attachment_threshold = 8000
    preview_chars = 1000

    def __init__(self, session_id: str | None = None, root: Path | None = None) -> None:
        self.root = root if root is not None else self.DEFAULT_ROOT
        self.id = session_id or self._unique_id(self.root)
        self.dir = self.root / self.id
        self.attachments_dir = self.dir / "attachments"
        self.conversation_path = self.dir / "conversation.jsonl"

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
        return path

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
