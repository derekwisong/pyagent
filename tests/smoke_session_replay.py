"""Audit attachment rehydration on session resume.

Locks three invariants so a future change can't silently regress the
context-management work from #4 / #5:

  1. **Stub-not-content** — when a tool result is offloaded, the JSONL
     transcript holds the stub string only. The full attachment
     content lives on disk in `attachments/` and is never serialized
     into `conversation.jsonl`.

  2. **Round-trip** — `Session.load_history()` returns precisely what
     `append_history()` wrote, with no transformation.

  3. **Single source of `Attachment` content in the conversation** —
     the only path by which a tool can produce attachment-style output
     is `Agent._render_tool_result`, which writes to disk and returns
     a stub. A grep guard fires if a new code site constructs
     `Attachment(...)` outside of `tools.py` (the read_file binary
     branch is the sole legitimate site at HEAD), or if the conversion
     in `_render_tool_result` is bypassed.

Originally raised in issue #6, sub-task 3. Run with:

    .venv/bin/python -m tests.smoke_session_replay
"""

from __future__ import annotations

import ast
import json
import re
import tempfile
from pathlib import Path

from pyagent.agent import Agent
from pyagent.session import Attachment, Session


def _check_stub_not_content() -> None:
    """JSONL persists offload stubs, never attachment payloads."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="audit", root=tmp)
    session._ensure_dirs()

    # Write a real, large attachment to disk — the kind that would
    # otherwise tempt anything in the pipeline to inline content.
    payload = "X" * 50_000
    attachment_path = session.write_attachment("read_file", payload)
    assert attachment_path.exists(), attachment_path
    assert attachment_path.read_text() == payload

    # Mint a stub the same way Agent._render_tool_result does.
    preview = payload[: session.preview_chars]
    stub = Agent._format_offload_ref(attachment_path, len(payload), preview)
    assert stub.startswith("[offload "), stub

    # Build a realistic tool-result conversation entry and persist it.
    entry = {
        "role": "user",
        "tool_results": [
            {"id": "call_1", "name": "read_file", "content": stub}
        ],
    }
    session.append_history([entry])

    # The persisted entry must equal the input plus a write-time
    # `ts` field — no other transformation. Compare structurally
    # rather than byte-equal so the timestamp doesn't make the
    # round-trip check brittle.
    raw = session.conversation_path.read_text()
    persisted = json.loads(raw)
    assert "ts" in persisted, (
        f"every appended entry should carry write-time `ts`: {persisted!r}"
    )
    persisted_no_ts = {k: v for k, v in persisted.items() if k != "ts"}
    assert persisted_no_ts == entry, (
        f"JSONL entry transformed beyond ts injection.\n"
        f"expected (modulo ts): {entry!r}\n"
        f"got:                  {persisted_no_ts!r}"
    )

    # The 50_000-char payload must not appear anywhere on the line.
    assert payload not in raw, (
        "attachment payload leaked into conversation.jsonl"
    )

    # Defense-in-depth: the largest single string on the JSONL line
    # should be the stub itself, well under any plausible attachment.
    # Anything > 5_000 chars is suspicious for a stub-only entry.
    longest_string = max(
        (m.group(0) for m in re.finditer(r'"(?:[^"\\]|\\.)*"', raw)),
        key=len,
    )
    assert len(longest_string) <= 5_000, (
        f"unexpectedly long string on JSONL line: {len(longest_string)} chars"
    )

    print(f"✓ stub-not-content: JSONL is {len(raw)} bytes, payload absent")


def _check_round_trip() -> None:
    """load_history returns the same payload append_history wrote,
    plus a `ts` write-time timestamp on each dict entry. The `ts`
    is added only on disk — the in-memory entry the caller passed
    is never mutated."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="rt", root=tmp)

    entries = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "ack",
            "tool_calls": [
                {"id": "c1", "name": "read_file", "args": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "tool_results": [
                {"id": "c1", "name": "read_file", "content": "[stub...]"}
            ],
        },
    ]
    in_snapshot = [dict(e) for e in entries]
    session.append_history(entries)
    assert entries == in_snapshot, (
        f"in-memory entries mutated: {entries} != {in_snapshot}"
    )
    loaded = session.load_history()
    assert all("ts" in e for e in loaded), (
        f"every loaded entry must carry a `ts`: {loaded}"
    )
    stripped = [
        {k: v for k, v in e.items() if k != "ts"} for e in loaded
    ]
    assert stripped == entries, (
        f"round-trip mismatch (ignoring ts):\n  in:  {entries}\n  "
        f"out: {stripped}"
    )
    print(f"✓ round-trip: {len(loaded)} entries identical (modulo ts)")


def _check_timestamps_preserved_and_monotonic() -> None:
    """Two separate `append_history` batches get distinct, ordered
    `ts` values; an entry that already carries `ts` is passed
    through untouched (caller-stamped events stay caller-stamped)."""
    import datetime as _dt
    import time

    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-ts-"))
    session = Session(session_id="ts", root=tmp)

    session.append_history([{"role": "user", "content": "first"}])
    time.sleep(0.005)  # ensure microsecond clock advances
    session.append_history(
        [
            {"role": "assistant", "content": "second"},
            # Pre-stamped: must survive untouched.
            {"role": "user", "content": "third", "ts": "preset"},
        ]
    )

    loaded = session.load_history()
    ts1 = loaded[0]["ts"]
    ts2 = loaded[1]["ts"]
    assert ts1 != ts2, f"separate writes should get distinct ts: {ts1!r}"
    # Lexicographic = chronological for ISO 8601 UTC.
    assert ts1 < ts2, f"ts not monotonic: {ts1!r} >= {ts2!r}"
    # Both auto-stamped values must parse as UTC ISO 8601.
    for ts in (ts1, ts2):
        parsed = _dt.datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, f"ts must be timezone-aware: {ts!r}"
    assert loaded[2]["ts"] == "preset", (
        f"caller-supplied ts must pass through, got {loaded[2]['ts']!r}"
    )
    print("✓ timestamps: ISO-UTC, monotonic across writes, caller-supplied preserved")


def _check_attachment_construction_sites() -> None:
    """Attachment is only constructed at the known read_file site.

    Uses AST so docstring/comment mentions don't trigger; only real
    `Attachment(...)` call expressions count.
    """
    repo_root = Path(__file__).resolve().parent.parent
    pyagent_dir = repo_root / "pyagent"
    sites: list[tuple[Path, int]] = []
    for py in pyagent_dir.rglob("*.py"):
        try:
            text = py.read_text()
        except (UnicodeDecodeError, PermissionError):
            continue
        try:
            tree = ast.parse(text, filename=str(py))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id if isinstance(fn, ast.Name)
                else fn.attr if isinstance(fn, ast.Attribute)
                else None
            )
            if name == "Attachment":
                sites.append((py.relative_to(repo_root), node.lineno))

    # Files whose Attachment(...) calls have been reviewed and confirmed
    # to flow through Agent._render_tool_result (i.e. they're returned
    # from a tool, not stuffed into the conversation directly).
    #   - pyagent/tools.py: read_file binary branch.
    #   - pyagent/plugins/web_search/__init__.py: side-saves the
    #     structured SearchResult list as JSON via Attachment(
    #     content=json, inline_text=markdown). The Agent layer
    #     renders inline_text + [also saved: ...] footer.
    #   - pyagent/plugins/reddit_search/__init__.py: same pattern as
    #     web_search — side-saves structured RedditPost list.
    #   - pyagent/plugins/hn_search/__init__.py: same pattern as
    #     web_search — side-saves structured HNStory list.
    allowed = {
        Path("pyagent/tools.py"),
        Path("pyagent/plugins/web_search/__init__.py"),
        Path("pyagent/plugins/reddit_search/__init__.py"),
        Path("pyagent/plugins/hn_search/__init__.py"),
    }
    unexpected = [(p, i) for (p, i) in sites if p not in allowed]
    assert not unexpected, (
        "Unexpected Attachment(...) construction site(s); each new site "
        "must be reviewed for whether its content can leak into "
        "conversation.jsonl bypassing Agent._render_tool_result:\n"
        + "\n".join(f"  {p}:{i}" for (p, i) in unexpected)
    )
    assert any(p == Path("pyagent/tools.py") for (p, _) in sites), (
        "expected at least one Attachment(...) site in pyagent/tools.py "
        "(read_file binary branch); none found — has the offload path moved?"
    )
    print(f"✓ Attachment construction sites: {[(str(p), i) for (p, i) in sites]}")


def _check_render_path_returns_stub() -> None:
    """Agent._render_tool_result with a Session converts Attachment → stub.

    This locks the conversion: even if a new tool returned an Attachment,
    the path through _render_tool_result writes to disk and returns the
    stub string — there is no in-memory return that carries raw content.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="render", root=tmp)
    agent = Agent(client=None, session=session)  # client unused here

    big = "Y" * 20_000
    rendered = agent._render_tool_result(
        "read_file", Attachment(content=big, preview=big[:100])
    )
    assert isinstance(rendered, str), type(rendered)
    assert big not in rendered, "raw content leaked through _render_tool_result"
    assert rendered.startswith("[offload "), rendered
    print(f"✓ _render_tool_result(Attachment) → stub: {len(rendered)} chars")


def _check_attachment_inline_text_unset_unchanged() -> None:
    """Regression-guard: Attachment without inline_text uses the same
    offload-header rendering as before #88. Locks "today's behavior is
    unchanged when the new field is None"."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="legacy", root=tmp)
    agent = Agent(client=None, session=session)

    payload = "Z" * 12_000
    rendered = agent._render_tool_result(
        "read_file",
        Attachment(content=payload, preview=payload[:100]),
    )
    assert rendered.startswith("[offload "), rendered
    assert "[also saved:" not in rendered, (
        "side-data footer must NOT appear when inline_text is None"
    )
    assert payload not in rendered
    print("✓ inline_text=None: legacy offload-header path unchanged")


def _check_attachment_inline_text_set() -> None:
    """When inline_text is set, the rendered output starts with the
    inline_text and ends with `[also saved: <path>]`. The file lands
    on disk with the expected `content`."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="inline", root=tmp)
    agent = Agent(client=None, session=session)

    inline = "## Top hits\n\n- foo (https://a)\n- bar (https://b)\n"
    structured = '[{"title": "foo", "url": "https://a"}]'
    rendered = agent._render_tool_result(
        "web_search",
        Attachment(
            content=structured,
            inline_text=inline,
            suffix=".json",
        ),
    )
    assert isinstance(rendered, str), type(rendered)
    assert rendered.startswith(inline), rendered
    # Footer at the tail; explicit about "complete above" + "for chaining".
    assert rendered.rstrip().endswith("]"), rendered
    assert "[also saved: " in rendered, rendered
    # The footer must signal that the inline view is COMPLETE so the
    # agent doesn't reflexively re-read the attachment for "missing"
    # content (a real risk given how the offload header trains
    # similar-shaped attention).
    assert "inline answer above is complete" in rendered, rendered
    assert "for downstream tools" in rendered, rendered
    # No offload header: the file is *side data*, not an offloaded big
    # result whose preview the agent must not re-read.
    assert "[offload " not in rendered, rendered
    assert "Do NOT read_file" not in rendered

    # Recover the path from the footer and confirm the bytes landed.
    # Footer shape: "[also saved: <path> — inline answer above is ...]"
    footer_chunk = rendered.rsplit("[also saved: ", 1)[1]
    saved_path = Path(footer_chunk.split(" — ", 1)[0])
    assert saved_path.exists(), saved_path
    assert saved_path.read_text() == structured
    assert saved_path.suffix == ".json", saved_path
    print(
        f"✓ inline_text path: rendered starts with inline_text, "
        f"ends with [also saved: ...], file on disk = "
        f"{saved_path.stat().st_size} bytes"
    )


def _check_attachment_inline_text_no_session() -> None:
    """No session → inline_text wins over preview/content. Plugins that
    optimistically build both shapes still degrade to the human view."""
    agent = Agent(client=None, session=None)
    inline = "## summary\nfoo\n"
    rendered = agent._render_tool_result(
        "web_search",
        Attachment(content="raw", inline_text=inline, preview="ignored"),
    )
    assert rendered == inline, rendered
    print("✓ inline_text path with no session: returns inline_text only")


def _check_attachment_metadata_side_channel() -> None:
    """When `_render_tool_result` offloads to a session attachment,
    it sets `agent._last_tool_attachment` so the agent loop can
    surface the path + size as a structured field on the
    tool_result entry — no need to regex `content`. When the result
    stays inline, the side channel resets to None.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-attmeta-"))
    session = Session(session_id="meta", root=tmp)
    agent = Agent(client=None, session=session)

    # Big plain-string result → auto-offload path.
    big = "X" * 20_000
    rendered = agent._render_tool_result("read_file", big)
    assert rendered.startswith("[offload "), rendered
    meta = agent._last_tool_attachment
    assert isinstance(meta, dict), meta
    assert meta["size_bytes"] == 20_000, meta
    assert Path(meta["path"]).exists(), meta
    assert meta["path"] in rendered, (
        f"path must still appear in content for the LLM, but got: "
        f"path={meta['path']!r} content prefix={rendered[:200]!r}"
    )

    # Attachment-typed result, no inline_text → offload path,
    # metadata still surfaces.
    rendered2 = agent._render_tool_result(
        "fetch_url",
        Attachment(content="A" * 9_000, preview="A" * 100),
    )
    assert rendered2.startswith("[offload "), rendered2
    meta2 = agent._last_tool_attachment
    assert meta2 and meta2["size_bytes"] == 9_000, meta2

    # Attachment with inline_text → file is side data, metadata
    # still surfaces (audit tools want to find the side file too).
    rendered3 = agent._render_tool_result(
        "web_search",
        Attachment(
            content='[{"title": "x"}]',
            inline_text="## hits\n- x\n",
            suffix=".json",
        ),
    )
    meta3 = agent._last_tool_attachment
    assert meta3 and meta3["path"].endswith(".json"), meta3
    assert meta3["size_bytes"] == len('[{"title": "x"}]'), meta3

    # Inline-only result (small string, no offload) → side channel
    # resets to None so the next tool_result entry doesn't inherit
    # a stale attachment from the previous call.
    rendered4 = agent._render_tool_result("execute", "ok")
    assert rendered4 == "ok"
    assert agent._last_tool_attachment is None, agent._last_tool_attachment

    print(
        "✓ attachment metadata side-channel: set on offload "
        "(plain-text + Attachment, with/without inline_text), "
        "cleared on inline-only result"
    )


def _check_attachment_field_reaches_tool_result_entry() -> None:
    """End-to-end: a tool that produces an over-threshold result
    causes the constructed tool_result entry to carry an
    `attachment` field with the same path the offload stub points
    at. Inline tool_result entries get no `attachment` field."""
    from pyagent.llms.pyagent import EchoClient

    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-attfield-"))
    session = Session(session_id="field", root=tmp)
    agent = Agent(client=EchoClient(), session=session)

    big = "Q" * 20_000

    def big_tool() -> str:
        """Returns over-threshold so it offloads."""
        return big

    def small_tool() -> str:
        """Returns under-threshold so it stays inline."""
        return "fine"

    agent.add_tool("big_tool", big_tool)
    agent.add_tool("small_tool", small_tool)

    # Drive _route_tool the same way the agent loop does, then
    # mirror the loop's entry-construction logic.
    def _build_entry(call: dict) -> dict:
        content = agent._route_tool(call)
        entry: dict = {
            "id": call["id"],
            "name": call["name"],
            "content": content,
        }
        if agent._last_tool_attachment is not None:
            entry["attachment"] = agent._last_tool_attachment
            agent._last_tool_attachment = None
        return entry

    big_entry = _build_entry(
        {"id": "c1", "name": "big_tool", "args": {}}
    )
    small_entry = _build_entry(
        {"id": "c2", "name": "small_tool", "args": {}}
    )

    assert "attachment" in big_entry, big_entry
    att = big_entry["attachment"]
    assert att["path"] in big_entry["content"], (
        f"attachment.path must match the offload stub in content: "
        f"path={att['path']!r} content={big_entry['content'][:200]!r}"
    )
    assert att["size_bytes"] == 20_000, att
    assert "attachment" not in small_entry, small_entry
    print(
        f"✓ tool_result entry: offloaded → has structured attachment "
        f"({att['size_bytes']}c at {Path(att['path']).name}); "
        f"inline → no attachment field"
    )


def main() -> None:
    _check_stub_not_content()
    _check_round_trip()
    _check_timestamps_preserved_and_monotonic()
    _check_attachment_construction_sites()
    _check_render_path_returns_stub()
    _check_attachment_inline_text_unset_unchanged()
    _check_attachment_inline_text_set()
    _check_attachment_inline_text_no_session()
    _check_attachment_metadata_side_channel()
    _check_attachment_field_reaches_tool_result_entry()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
