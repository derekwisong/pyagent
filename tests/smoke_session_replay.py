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

    # The raw JSONL line must equal `json.dumps(entry) + "\n"` —
    # nothing else, no transformation.
    raw = session.conversation_path.read_text()
    expected = json.dumps(entry, ensure_ascii=False) + "\n"
    assert raw == expected, (
        f"JSONL line was transformed.\n"
        f"expected: {expected!r}\n"
        f"got:      {raw!r}"
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
    """load_history returns exactly what append_history wrote."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-replay-"))
    session = Session(session_id="rt", root=tmp)

    entries = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "text": "ack",
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
    session.append_history(entries)
    loaded = session.load_history()
    assert loaded == entries, f"round-trip mismatch:\n  in:  {entries}\n  out: {loaded}"
    print(f"✓ round-trip: {len(loaded)} entries identical")


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

    allowed = {Path("pyagent/tools.py")}
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
    # Footer at the tail; minimal shape "[also saved: <path>]".
    assert rendered.rstrip().endswith("]"), rendered
    assert "[also saved: " in rendered, rendered
    # No offload header: the file is *side data*, not an offloaded big
    # result whose preview the agent must not re-read.
    assert "[offload " not in rendered, rendered
    assert "Do NOT read_file" not in rendered

    # Recover the path from the footer and confirm the bytes landed.
    footer = rendered.rsplit("[also saved: ", 1)[1].rstrip().rstrip("]")
    saved_path = Path(footer)
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


def main() -> None:
    _check_stub_not_content()
    _check_round_trip()
    _check_attachment_construction_sites()
    _check_render_path_returns_stub()
    _check_attachment_inline_text_unset_unchanged()
    _check_attachment_inline_text_set()
    _check_attachment_inline_text_no_session()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
