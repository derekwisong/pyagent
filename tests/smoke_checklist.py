"""Smoke for the agent-managed checklist (issue #38).

Locks the contract for `pyagent.checklist.Checklist`, the three
tool factories that bind to it, the `checklist` event flowing into
`_update_agents_state`, and the footer rendering produced by
`_render_status` when a checklist is live.

Run with:

    .venv/bin/python -m tests.smoke_checklist
"""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path

from rich.console import Console

from pyagent.checklist import (
    Checklist,
    make_add_task,
    make_list_tasks,
    make_update_task,
)
from pyagent.cli import (
    _CHECKLIST_TITLE_MAX,
    _checklist_segment,
    _print_tasks,
    _render_status,
    _update_agents_state,
)


def render_plain(markup: str) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, color_system=None).print(markup)
    return buf.getvalue().rstrip()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "checklist.json"

        # 1. Empty checklist persists nothing yet, summary is None.
        emitted: list[list[dict]] = []
        cl = Checklist(path, on_change=lambda tasks: emitted.append(tasks))
        assert cl.list() == []
        assert cl.summary() is None
        assert not path.exists()
        print("✓ empty checklist: no file, no summary")

        # 2. add → file exists, summary reports 0/N with current title,
        #    on_change fired with the snapshot.
        a = cl.add("write migration")
        b = cl.add("run tests")
        c = cl.add("update README")
        assert a["id"] == "t-1" and a["status"] == "pending"
        assert path.exists()
        assert len(emitted) == 3, f"on_change fired {len(emitted)}x"
        s = cl.summary()
        assert s == {
            "completed": 0,
            "total": 3,
            "current_title": "write migration",
        }, s
        print(f"✓ add: {s}")

        # 3. update to in_progress shifts the current title to the
        #    newly active task.
        cl.update("t-1", "in_progress")
        s = cl.summary()
        assert s["current_title"] == "write migration", s
        # Move t-1 to completed; current should fall through to t-2 (pending).
        cl.update("t-1", "completed")
        s = cl.summary()
        assert s == {
            "completed": 1,
            "total": 3,
            "current_title": "run tests",
        }, s
        print(f"✓ update progresses current: {s}")

        # 4. Cancelled task drops out of total and current.
        cl.update("t-2", "cancelled", note="superseded by manual fix")
        s = cl.summary()
        assert s == {
            "completed": 1,
            "total": 2,
            "current_title": "update README",
        }, s
        print(f"✓ cancelled excluded from total: {s}")

        # 5. All-complete summary returns None (footer drops segment).
        cl.update("t-3", "completed")
        assert cl.summary() is None, cl.summary()
        print("✓ all complete → summary None")

        # 6. Resume: a new Checklist on the same path picks up state
        #    AND continues the id sequence past the existing max.
        cl2 = Checklist(path)
        assert [t["id"] for t in cl2.list()] == ["t-1", "t-2", "t-3"]
        d = cl2.add("post-merge cleanup")
        assert d["id"] == "t-4", d
        print(f"✓ resume: ids continue → {d['id']}")

        # 7. Bad inputs raise — caller (the agent) sees the error.
        try:
            cl2.update("t-1", "bogus_status")
        except ValueError as e:
            assert "status must be one of" in str(e)
            print(f"✓ bad status raises: {e}")
        else:
            raise AssertionError("expected ValueError on bad status")
        try:
            cl2.update("t-999", "completed")
        except KeyError as e:
            assert "t-999" in str(e)
            print(f"✓ unknown id raises: {e}")
        else:
            raise AssertionError("expected KeyError on unknown id")
        try:
            cl2.add("   ")
        except ValueError as e:
            assert "title is required" in str(e)
            print(f"✓ empty title raises: {e}")
        else:
            raise AssertionError("expected ValueError on empty title")

        # 8. JSON on disk round-trips cleanly (next_id preserved).
        data = json.loads(path.read_text())
        assert data["next_id"] == 5, data
        assert len(data["tasks"]) == 4, data
        print(f"✓ disk format: next_id={data['next_id']}")

    # 9. Tool factories: add_task / update_task / list_tasks behave
    #    as the model would experience them.
    with tempfile.TemporaryDirectory() as tmpdir:
        cl = Checklist(Path(tmpdir) / "checklist.json")
        add = make_add_task(cl)
        upd = make_update_task(cl)
        lst = make_list_tasks(cl)
        t = add("design schema")
        assert t["id"] == "t-1" and t["title"] == "design schema"
        upd(t["id"], "in_progress")
        upd(t["id"], "completed", note="reviewed in PR")
        rows = lst()
        assert rows[0]["status"] == "completed"
        assert rows[0]["note"] == "reviewed in PR"
        print("✓ tool factories: add/update/list end-to-end")

    # 10. CLI footer integration: checklist event populates root,
    #     _render_status shows the segment, all-complete drops it.
    agents: dict = {"root": {"status": "thinking"}}
    _update_agents_state(
        agents,
        {
            "type": "checklist",
            "tasks": [
                {"id": "t-1", "title": "write migration",
                 "status": "in_progress", "note": ""},
                {"id": "t-2", "title": "run tests",
                 "status": "pending", "note": ""},
                {"id": "t-3", "title": "update README",
                 "status": "pending", "note": ""},
            ],
        },
    )
    out = render_plain(_render_status(agents))
    assert "0/3" in out and "write migration" in out, out
    assert out.startswith("thinking…"), out
    print(f"✓ footer (in_progress): {out!r}")

    # Move first to completed → counts and current update.
    _update_agents_state(
        agents,
        {
            "type": "checklist",
            "tasks": [
                {"id": "t-1", "title": "write migration",
                 "status": "completed", "note": ""},
                {"id": "t-2", "title": "run tests",
                 "status": "in_progress", "note": ""},
                {"id": "t-3", "title": "update README",
                 "status": "pending", "note": ""},
            ],
        },
    )
    out = render_plain(_render_status(agents))
    assert "1/3" in out and "run tests" in out, out
    print(f"✓ footer (advance): {out!r}")

    # All-complete → segment vanishes.
    _update_agents_state(
        agents,
        {
            "type": "checklist",
            "tasks": [
                {"id": "t-1", "title": "x",
                 "status": "completed", "note": ""},
                {"id": "t-2", "title": "y",
                 "status": "completed", "note": ""},
            ],
        },
    )
    seg = _checklist_segment(agents)
    assert seg == "", repr(seg)
    out = render_plain(_render_status(agents))
    assert out == "thinking…", out
    print("✓ footer drops segment when all complete")

    # 11. Long titles get truncated in the footer (not in /tasks).
    long_title = "x" * (_CHECKLIST_TITLE_MAX + 20)
    _update_agents_state(
        agents,
        {
            "type": "checklist",
            "tasks": [
                {"id": "t-1", "title": long_title,
                 "status": "in_progress", "note": ""},
                {"id": "t-2", "title": "next",
                 "status": "pending", "note": ""},
            ],
        },
    )
    seg = _checklist_segment(agents)
    assert "…" in seg, seg
    assert len(seg) < len(long_title) + 20, seg
    print(f"✓ long title truncated in footer: ...{seg[-30:]!r}")

    # 12. /tasks renders full list including notes (no truncation).
    _update_agents_state(
        agents,
        {
            "type": "checklist",
            "tasks": [
                {"id": "t-1", "title": "design schema",
                 "status": "completed", "note": "reviewed in PR"},
                {"id": "t-2", "title": "write migration",
                 "status": "in_progress", "note": ""},
                {"id": "t-3", "title": "abandoned approach",
                 "status": "cancelled", "note": "supplanted by t-2"},
            ],
        },
    )
    buf = io.StringIO()
    captured = Console(file=buf, force_terminal=False, color_system=None)
    # Patch the module's `console` so _print_tasks renders into our buf.
    import pyagent.cli as cli_mod
    real_console = cli_mod.console
    cli_mod.console = captured
    try:
        _print_tasks(agents)
    finally:
        cli_mod.console = real_console
    out = buf.getvalue()
    assert "design schema" in out and "write migration" in out, out
    assert "abandoned approach" in out, out
    assert "reviewed in PR" in out, out
    assert "✓" in out and "▶" in out and "✗" in out, out
    print("✓ /tasks renders full list with status glyphs and notes")

    # 13. /tasks on empty list prints "no tasks".
    agents_empty: dict = {"root": {"status": "thinking"}}
    buf = io.StringIO()
    captured = Console(file=buf, force_terminal=False, color_system=None)
    cli_mod.console = captured
    try:
        _print_tasks(agents_empty)
    finally:
        cli_mod.console = real_console
    assert "no tasks" in buf.getvalue(), buf.getvalue()
    print("✓ /tasks on empty: 'no tasks'")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
