"""Agent-managed task checklist.

A small, flat list of tasks the model maintains over the course of a
multi-step turn. The model uses the `add_task` / `update_task` /
`list_tasks` tools to drive it; the CLI reads the same state to render
a progress segment in the status footer.

State is per-session — persisted to `<session.dir>/checklist.json` so
`--resume` brings the list back. Snapshots-on-mutation (atomic rename)
rather than append-only JSONL because the typical task count is small
(≤ ~20) and in-place mutation is the dominant op; replaying an event
log to reconstruct state would be all cost, no benefit.

Statuses are flat: `pending`, `in_progress`, `completed`, `cancelled`.
There is intentionally no nesting — flat lists are what the model
handles well and what the footer can render.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable

VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")


class Checklist:
    """Session-scoped checklist with atomic JSON persistence.

    Thread-safe (a `_lock` serializes all mutations and the load/save
    pair) so the agent's tool-execution thread can't race with anything
    else looking at `tasks`. The instance owns its persistence path —
    callers don't write the file directly.
    """

    def __init__(
        self,
        path: Path,
        on_change: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> None:
        self.path = path
        self._on_change = on_change
        self._lock = threading.Lock()
        self.tasks: list[dict[str, Any]] = []
        self._next_id: int = 1
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        tasks = data.get("tasks") or []
        if isinstance(tasks, list):
            self.tasks = [t for t in tasks if isinstance(t, dict)]
        nxt = data.get("next_id")
        if isinstance(nxt, int) and nxt > 0:
            self._next_id = nxt
        else:
            # Recover from a malformed file: pick max-id + 1 so a new
            # add doesn't collide with an existing task.
            mx = 0
            for t in self.tasks:
                tid = t.get("id", "")
                if isinstance(tid, str) and tid.startswith("t-"):
                    try:
                        mx = max(mx, int(tid[2:]))
                    except ValueError:
                        pass
            self._next_id = mx + 1

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"tasks": self.tasks, "next_id": self._next_id}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, self.path)

    def _notify(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(list(self.tasks))
        except Exception:
            # The change-listener (event emit) must never break a tool.
            pass

    def add(self, title: str) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("title is required")
        with self._lock:
            tid = f"t-{self._next_id}"
            self._next_id += 1
            entry = {"id": tid, "title": title, "status": "pending", "note": ""}
            self.tasks.append(entry)
            self._save()
        self._notify()
        return dict(entry)

    def update(
        self, id: str, status: str, note: str | None = None
    ) -> dict[str, Any]:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"status must be one of {VALID_STATUSES!r}, got {status!r}"
            )
        with self._lock:
            for t in self.tasks:
                if t.get("id") == id:
                    t["status"] = status
                    if note is not None:
                        t["note"] = note
                    self._save()
                    found = dict(t)
                    break
            else:
                raise KeyError(f"no task with id {id!r}")
        self._notify()
        return found

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(t) for t in self.tasks]

    def summary(self) -> dict[str, Any] | None:
        """Return the footer-friendly digest, or None for no surfaceable state.

        Counts: `completed` and `total` ignore cancelled tasks (they're
        neither in-progress nor a denominator the user cares about).
        `current_title` picks the task `in_progress`; if none, the next
        `pending`. Returns None when the list is empty or every task is
        completed/cancelled — caller drops the segment.
        """
        with self._lock:
            tasks = list(self.tasks)
        if not tasks:
            return None
        total = sum(1 for t in tasks if t.get("status") != "cancelled")
        completed = sum(1 for t in tasks if t.get("status") == "completed")
        if total == 0 or completed == total:
            return None
        current = next(
            (t for t in tasks if t.get("status") == "in_progress"),
            None,
        ) or next(
            (t for t in tasks if t.get("status") == "pending"),
            None,
        )
        current_title = current.get("title", "") if current else ""
        return {
            "completed": completed,
            "total": total,
            "current_title": current_title,
        }


def make_add_task(checklist: Checklist) -> Callable[..., dict[str, Any]]:
    def add_task(title: str) -> dict[str, Any]:
        """Append a new task to the session checklist.

        Use this BEFORE starting a multi-step job (≥3 distinct
        subtasks) so the user can see what you intend to do and you
        can self-monitor across tool calls. Skip the checklist
        entirely for one-shot work — a single file edit, a one-line
        question, a quick lookup. Indiscriminate use is worse than
        none.

        Each task is a short imperative phrase (e.g. "write
        migration", "run tests", "update README"), not a long
        sentence — the title appears in a one-line footer. Returns
        the new task's id; pass that id to `update_task` later to
        change its status.
        """
        return checklist.add(title)

    return add_task


def make_update_task(checklist: Checklist) -> Callable[..., dict[str, Any]]:
    def update_task(
        id: str, status: str, note: str = ""
    ) -> dict[str, Any]:
        """Change a task's status (and optionally attach a note).

        `status` is one of: `pending`, `in_progress`, `completed`,
        `cancelled`. Discipline that makes the checklist work:

        - **Exactly one task `in_progress` at a time.** Move the
          previous one to `completed` (or `cancelled`) before
          starting the next. Two tasks "in_progress" at once means
          you're not really tracking either.
        - **Mark `completed` immediately when done — don't batch.**
          Updating five tasks at the end of a turn defeats the point;
          the user can't see progress and you can't catch yourself
          drifting.
        - **Use `cancelled` (with a `note`) when you abandon a step**
          — e.g. the approach turned out to be wrong. Don't silently
          leave it `pending`.

        `note` is freeform context: a blocker, a decision, why you
        cancelled. Empty string leaves the existing note unchanged.
        """
        return checklist.update(id, status, note=note if note else None)

    return update_task


def make_list_tasks(checklist: Checklist) -> Callable[..., list[dict[str, Any]]]:
    def list_tasks() -> list[dict[str, Any]]:
        """Return the current checklist (id, title, status, note).

        Useful when re-orienting after a long tool result or a
        cancelled subagent — confirm what's still pending before
        deciding what to do next.
        """
        return checklist.list()

    return list_tasks
