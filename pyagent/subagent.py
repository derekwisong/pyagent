"""Subagent registry types and meta-tool factories.

Subagents are full agents in their own subprocesses, spawned by an
existing agent process via `multiprocessing.spawn` (the same path the
CLI uses to spawn the root agent). Each subagent's pipe is multiplexed
into the parent's IO loop; turn replies land on a per-subagent reply
queue that `call_subagent` blocks on.

This module defines:
  - `SubagentEntry` — the record stored in `Agent._subagents`
  - `make_spawn_subagent` / `make_call_subagent` / `make_terminate_subagent`
    — factories that close over the parent's `_ChildState`, `Agent`,
    base config, and parent session, and return the actual tool
    callables to be registered via `Agent.add_tool`.

Caps are read from `pyagent.config` at registration time and enforced
inside `spawn_subagent`. Refusal returns a leading-`<` error marker so
the model sees the limit and adapts.

Subagents are recursive — every subagent gets the meta-tools
registered, so a child can spawn its own children. Bounded by
`subagents.max_depth` and `subagents.max_fanout` from config.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import queue
import signal
import uuid
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from typing import TYPE_CHECKING, Any, Callable

from pyagent import config as config_mod
from pyagent import permissions
from pyagent import protocol
from pyagent import roles as roles_mod

if TYPE_CHECKING:
    from pyagent.agent import Agent
    from pyagent.agent_proc import _ChildState
    from pyagent.session import Session


logger = logging.getLogger(__name__)


@dataclass
class SubagentEntry:
    id: str
    name: str
    process: SpawnProcess
    conn: Connection
    reply_queue: queue.Queue
    depth: int
    status: str = "idle"  # idle | running | done | error
    last_text: str = ""
    # Dispatch mode for the most-recent in-flight call to this
    # subagent. None when no call is in flight; "sync" while a
    # call_subagent is blocking on the reply queue; "async" while a
    # call_subagent_async has fired and not yet replied. The IO
    # thread reads this when a turn_complete arrives to decide
    # whether to land the reply on the per-sid reply queue (sync)
    # or on the parent Agent's pending_async_replies inbox (async).
    mode: str | None = None


def _build_subagent_config(
    name: str,
    system_prompt: str,
    base_config: dict[str, Any],
    parent_session: "Session",
    parent_depth: int,
    model_override: str = "",
    role: roles_mod.Role | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the subagent's spawn config. Returns (subagent_id, config)."""
    sid = f"{name}-{uuid.uuid4().hex[:8]}"
    session_root = parent_session.dir / "subagents"
    cfg = dict(base_config)
    cfg["session_id"] = sid
    cfg["session_root"] = str(session_root)
    cfg["depth"] = parent_depth + 1
    cfg["parent_session_id"] = parent_session.id
    cfg["task_body"] = system_prompt
    cfg["is_subagent"] = True
    if model_override:
        cfg["model"] = model_override
    if role is not None:
        cfg["role_body"] = role.system_prompt
        cfg["role_tools"] = list(role.tools) if role.tools is not None else None
        cfg["role_meta_tools"] = role.meta_tools
    # Inherit current approved paths so the user isn't re-prompted for
    # paths they already accepted in the parent's process.
    cfg["approved_paths"] = [str(p) for p in permissions.approved_paths()]
    return sid, cfg


def make_spawn_subagent(
    state: "_ChildState",
    agent: "Agent",
    parent_session: "Session",
    base_config: dict[str, Any],
) -> Callable[..., str]:
    """Build the spawn_subagent tool, closing over the parent's state."""
    cfg = config_mod.load()
    max_depth = cfg["subagents"]["max_depth"]
    max_fanout = cfg["subagents"]["max_fanout"]

    def spawn_subagent(
        name: str, system_prompt: str, model: str = ""
    ) -> str:
        """Spawn a subagent in its own subprocess.

        The subagent inherits the universal SOUL/TOOLS/PRIMER base, the
        live skills catalog, and the core tool set (read_file,
        write_file, list_directory, grep, execute, fetch_url,
        read_skill, plus spawn/call/terminate so it can fan out
        further if the depth cap allows). Memory tools are root-only;
        a subagent doesn't get USER/MEMORY access. Use
        `call_subagent(<id>, message)` to send it work and
        `terminate_subagent(<id>)` when you're done.

        Args:
            name: Short label for this subagent (e.g. "researcher",
                "fact-checker"). Becomes part of the subagent id and
                the CLI's display prefix for events from this child.
            system_prompt: The *task* description for this subagent —
                what you want it to do, what to focus on, what to
                ignore. Do NOT restate persona, voice, tool semantics,
                or operating principles; the subagent already inherits
                all of that. Keep this short and task-shaped.
            model: Optional. Either a role name (looked up in the
                "Available subagent models" catalog) or a raw
                provider/model string ("anthropic/claude-opus-4-7",
                "openai/gpt-4o"). When a role is named, the subagent
                also inherits the role's default persona and tool
                allowlist. Empty (the default) inherits the parent's
                model with no role specialization.

        Returns:
            The subagent id string on success, or an error marker
            string starting with "<" if a cap is hit or the spawn fails.
        """
        if agent.depth + 1 > max_depth:
            return (
                f"<refused: would exceed max_depth={max_depth} "
                f"(spawning agent depth={agent.depth})>"
            )
        if len(agent._subagents) >= max_fanout:
            return (
                f"<refused: at max_fanout={max_fanout} live subagents; "
                f"terminate one before spawning another>"
            )

        try:
            resolved_model, role = roles_mod.resolve(model)
        except ValueError as e:
            return f"<refused: bad model {model!r}: {e}>"

        sid, sub_config = _build_subagent_config(
            name=name,
            system_prompt=system_prompt,
            base_config=base_config,
            parent_session=parent_session,
            parent_depth=agent.depth,
            model_override=resolved_model,
            role=role,
        )

        ctx = multiprocessing.get_context("spawn")
        parent_end, child_end = ctx.Pipe(duplex=True)

        # Late import to avoid an agent_proc <-> subagent cycle at
        # module-import time.
        from pyagent import agent_proc

        proc = ctx.Process(
            target=agent_proc.child_main,
            args=(sub_config, child_end),
            name=f"pyagent-subagent-{name}",
            daemon=False,
        )
        proc.start()
        child_end.close()

        reply_queue = state.register_subagent_pipe(sid, parent_end)

        entry = SubagentEntry(
            id=sid,
            name=name,
            process=proc,
            conn=parent_end,
            reply_queue=reply_queue,
            depth=agent.depth + 1,
        )
        agent._subagents[sid] = entry

        # Block on the subagent's `ready` event (or an `agent_error`
        # if bootstrap failed). The IO thread routes both to the
        # subagent's reply queue.
        try:
            first = reply_queue.get(timeout=30)
        except queue.Empty:
            agent._subagents.pop(sid, None)
            state.unregister_subagent_pipe(sid)
            try:
                proc.terminate()
            except Exception:
                pass
            return f"<spawn failed: {sid} did not become ready in 30s>"

        if first.get("type") == "agent_error":
            agent._subagents.pop(sid, None)
            state.unregister_subagent_pipe(sid)
            proc.join(timeout=2)
            return (
                f"<spawn failed: {first.get('kind')}: "
                f"{first.get('message')}>"
            )
        if first.get("type") != "ready":
            agent._subagents.pop(sid, None)
            state.unregister_subagent_pipe(sid)
            try:
                proc.terminate()
            except Exception:
                pass
            return f"<spawn failed: unexpected first event {first.get('type')!r}>"

        state.send(
            "info",
            level="info",
            message=f"spawned subagent {name} (id={sid}, depth={entry.depth})",
        )
        return sid

    return spawn_subagent


def make_call_subagent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the call_subagent tool."""

    def call_subagent(id: str, message: str) -> str:
        """Send a user prompt to a subagent and block until it finishes its turn.

        The subagent's intermediate events (assistant text, tool calls,
        permission prompts) are forwarded to the CLI as they happen, so
        the human can see what the subagent is doing in real time. Only
        the final aggregated assistant text returns through this call —
        that text is what shows up in the parent agent's tool_result.

        This call is synchronous: the parent's loop is paused until the
        subagent replies. If you want concurrency, design with care
        around the cost-amplification (each subagent burns its own LLM
        tokens).

        Args:
            id: The subagent id returned from `spawn_subagent`.
            message: The user-prompt text the subagent should process.

        Returns:
            The subagent's final assistant text, or an error marker.
        """
        entry: SubagentEntry | None = agent._subagents.get(id)
        if entry is None:
            return f"<unknown subagent id: {id!r}>"
        if not entry.process.is_alive():
            return f"<subagent {id} is no longer running>"

        # Defensive drain in case prior turns left anything behind.
        while not entry.reply_queue.empty():
            try:
                entry.reply_queue.get_nowait()
            except queue.Empty:
                break

        if entry.mode is not None:
            return (
                f"<subagent {id} is busy ({entry.mode}); "
                f"wait or terminate before calling again>"
            )
        entry.status = "running"
        entry.mode = "sync"
        try:
            protocol.send(entry.conn, "user_prompt", prompt=message)
        except (BrokenPipeError, OSError) as e:
            entry.status = "error"
            entry.mode = None
            return f"<send failed to subagent {id}: {e}>"

        # No timeout here — subagents can legitimately take a long
        # time. Cancel from the CLI propagates down through the IO
        # thread's cancel pathway.
        result = entry.reply_queue.get()
        entry.status = "idle"
        entry.mode = None
        kind = result.get("type")
        if kind == "turn_complete":
            text = result.get("final_text", "") or ""
            entry.last_text = text
            return text or "<subagent returned no text>"
        if kind == "agent_error":
            entry.status = "error"
            return (
                f"<subagent error: {result.get('kind')}: "
                f"{result.get('message')}>"
            )
        return f"<unexpected reply kind {kind!r}>"

    return call_subagent


def make_call_subagent_async(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the call_subagent_async tool.

    Fires a user_prompt at the subagent and returns immediately. The
    subagent's eventual final assistant text is delivered to the
    parent agent's `pending_async_replies` inbox by the IO thread,
    which means the parent's next LLM call will see it as a
    user-role message of the form `[subagent <name> (<id>) reports]:
    <text>`. Pair with `wait_for_subagents` to block until at least
    one reply is ready.
    """

    def call_subagent_async(id: str, message: str) -> str:
        """Send a message to a subagent and continue immediately.

        The subagent runs in parallel with the rest of your turn.
        Its reply will appear as a user-role message of the form
        `[subagent <name> (<id>) reports]: <text>` at the start of
        a future turn — specifically, before the next LLM call
        after the subagent finishes. Useful for fan-out workflows
        where you want multiple subagents working at once.

        Pair with `wait_for_subagents()` to block until at least
        one reply is ready, then read the synthesized user
        messages on the next turn.

        Args:
            id: The subagent id returned from `spawn_subagent`.
            message: The user-prompt text the subagent should
                process.

        Returns:
            Confirmation string. The actual reply does NOT come
            back through this call — it arrives as a synthesized
            user message later.
        """
        entry: SubagentEntry | None = agent._subagents.get(id)
        if entry is None:
            return f"<unknown subagent id: {id!r}>"
        if not entry.process.is_alive():
            return f"<subagent {id} is no longer running>"
        if entry.mode is not None:
            return (
                f"<subagent {id} is busy ({entry.mode}); "
                f"wait or terminate before calling again>"
            )

        entry.mode = "async"
        entry.status = "running"
        try:
            protocol.send(entry.conn, "user_prompt", prompt=message)
        except (BrokenPipeError, OSError) as e:
            entry.mode = None
            entry.status = "error"
            return f"<send failed to subagent {id}: {e}>"
        return (
            f"<async call queued to {id}; "
            f"its reply will arrive as a user message before a "
            f"future LLM turn — call wait_for_subagents() to block "
            f"until at least one is ready>"
        )

    return call_subagent_async


def make_wait_for_subagents(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the wait_for_subagents tool.

    Blocks the agent's main thread until at least one async-fired
    subagent reply is ready in the parent's inbox, the cancel event
    is set (Esc from the CLI), or the timeout expires.
    """
    import time as _time

    def wait_for_subagents(timeout: int = 300) -> str:
        """Block until at least one async subagent reply is ready.

        Use this after firing one or more `call_subagent_async` to
        pause your turn until results arrive. The replies will be
        delivered to your conversation as user-role messages at the
        start of the next turn — this tool just signals 'they're
        ready, read your inbox'.

        Returns immediately if there are already replies waiting.

        Args:
            timeout: Maximum seconds to wait. Default 300 (5
                minutes).

        Returns:
            How many replies are ready, a `<wait timed out>`
            marker, or `<wait cancelled>` if Esc was pressed.
        """
        deadline = _time.monotonic() + max(1, int(timeout))
        while _time.monotonic() < deadline:
            if not agent.pending_async_replies.empty():
                n = agent.pending_async_replies.qsize()
                return f"{n} subagent reply(s) ready — read on next turn"
            if state.cancel_event.is_set():
                return "<wait cancelled>"
            _time.sleep(0.1)
        return f"<wait timed out after {timeout}s>"

    return wait_for_subagents


def make_ask_parent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the `ask_parent` tool — only registered on subagents.

    Sends a question up to the immediate parent agent and blocks
    the subagent's tool call until the parent's `reply_to_subagent`
    answer arrives, or until the 5-minute timeout fires. Issue #47.

    The mechanics:
      - generate a request_id
      - create a Queue and register it under that id
      - emit `subagent_ask` upstream
      - block on Queue.get(timeout=300)
      - on timeout, return a `<no answer ...>` marker so the
        subagent can decide what to do (retry, give up, fail loudly)
    """
    _ASK_TIMEOUT_S = 300

    def ask_parent(question: str) -> str:
        """Ask the immediate parent agent for guidance, mid-task.

        Use this when you (a subagent) hit something you can't
        decide on your own and the parent has the context: an
        ambiguous spec, a missing dependency, a permission
        question, a tie-breaking judgment call. The parent sees
        your question as a user-role message at the start of its
        next turn and answers via `reply_to_subagent`.

        This is **synchronous** — your tool call blocks until the
        parent replies (or 5 minutes pass). If the parent is in
        the middle of its own turn, you wait for that turn to
        finish before it sees your question.

        Use sparingly. Each ask costs the parent a turn cycle and
        blocks your work. Don't ask for things you can answer
        yourself by reading the prompt or running a quick tool
        call.

        Args:
            question: Plain text. Be concrete and self-contained;
                the parent has its own context but doesn't have
                yours. Include any specifics it needs to answer
                without a follow-up round-trip.

        Returns:
            The parent's reply string, or `<no answer from parent
            within 300s>` on timeout.
        """
        question = (question or "").strip()
        if not question:
            return "<refused: empty question>"

        req_id = f"req-{uuid.uuid4().hex[:8]}"
        rq: queue.Queue = queue.Queue(maxsize=1)
        with state._ask_lock:
            # Refuse stacked asks — keep the model's reasoning
            # straightforward (one question at a time per subagent).
            if state._pending_ask_replies:
                return (
                    "<refused: another ask_parent is already in "
                    "flight; await its answer first>"
                )
            state._pending_ask_replies[req_id] = rq

        try:
            state.send(
                "subagent_ask", request_id=req_id, question=question
            )
        except Exception as e:
            with state._ask_lock:
                state._pending_ask_replies.pop(req_id, None)
            return f"<send failed: {type(e).__name__}: {e}>"

        try:
            answer = rq.get(timeout=_ASK_TIMEOUT_S)
        except queue.Empty:
            with state._ask_lock:
                state._pending_ask_replies.pop(req_id, None)
            return f"<no answer from parent within {_ASK_TIMEOUT_S}s>"

        return answer if answer else "<parent replied with empty answer>"

    return ask_parent


_NOTIFY_VALID_SEVERITIES = ("info", "warn", "alert")


def make_notify_parent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the `notify_parent` tool — only registered on subagents.

    Sends a non-blocking note up to the immediate parent agent and
    returns immediately. The parent's IO thread appends the note to
    a per-sid ring and queues a formatted user-role message onto the
    parent's `pending_async_replies`, surfacing at the parent's next
    LLM-call boundary. Issue #64.
    """

    def notify_parent(text: str, severity: str = "info") -> str:
        """Drop a non-blocking note to your immediate parent agent.

        Use this when you've learned something the parent should
        know but you don't need an answer to keep going. The parent
        will see your note as a user-role message at its next
        LLM-call boundary; it can act, defer, or ignore.

        Don't spam. One note should change the parent's behavior or
        understanding — if it wouldn't, don't send it. Good fits:
        a framing concern ("the test runner here is broken; switch
        approach"), a heads-up that supersedes earlier work, a
        completed milestone the parent is waiting on. Bad fits:
        progress chatter ("starting now", "still working"),
        restating the obvious, anything that reads like narration.

        Unlike `ask_parent`, this is fire-and-forget: there's no
        request_id and the parent never replies. Use `ask_parent`
        when you actually need a decision.

        Args:
            text: The note text. Plain prose, self-contained — the
                parent has its context but doesn't have yours.
            severity: One of "info", "warn", "alert". "alert"
                signals the parent should consider pivoting; "warn"
                flags a concern; "info" is everything else. Default
                "info".

        Returns:
            A short status string. Errors come back as `<...>`
            markers (empty text, unknown severity, send failure).
        """
        text = (text or "").strip()
        if not text:
            return "<refused: empty text>"
        if severity not in _NOTIFY_VALID_SEVERITIES:
            valid = ", ".join(repr(s) for s in _NOTIFY_VALID_SEVERITIES)
            return f"<refused: severity {severity!r} not in {{{valid}}}>"
        try:
            state.send(
                "subagent_note", severity=severity, text=text
            )
        except Exception as e:
            return f"<send failed: {type(e).__name__}: {e}>"
        return f"note sent ({severity})"

    return notify_parent


def make_reply_to_subagent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the `reply_to_subagent` tool — registered on agents
    that can spawn subagents (`allow_meta=True`).

    Looks up the recorded sid for `request_id`, sends a
    `parent_answer` event down the right pipe, and removes the
    pending entry from the registry. Issue #47.
    """

    def reply_to_subagent(request_id: str, answer: str) -> str:
        """Answer a subagent's `ask_parent` question.

        When a subagent calls `ask_parent(question)` mid-task, you
        see a user-role message of the form
        `[subagent <name> (<sid>) asks (req=<request_id>)]: <question>`
        at the start of your next turn. Extract the `request_id`
        from that message and pass it here along with your answer
        text. Your answer unblocks the subagent's tool call.

        Args:
            request_id: The `req-XXXXXXXX` id from the bracket of
                the inbound ask message.
            answer: Your reply to the subagent. Plain text. Be
                concrete — the subagent will likely act on it
                immediately.

        Returns:
            A short status string. Errors come back as `<...>`
            markers (unknown request_id, target subagent already
            terminated, etc.).
        """
        request_id = (request_id or "").strip()
        if not request_id:
            return "<refused: empty request_id>"
        with state._ask_lock:
            sid = state._inbound_ask_sid.pop(request_id, None)
        if sid is None:
            return (
                f"<unknown request_id {request_id!r}; either already "
                f"answered or never received>"
            )
        entry: SubagentEntry | None = agent._subagents.get(sid)
        if entry is None or not entry.process.is_alive():
            return (
                f"<subagent {sid} for request {request_id!r} is "
                f"no longer running>"
            )
        try:
            protocol.send(
                entry.conn,
                "parent_answer",
                request_id=request_id,
                answer=answer or "",
            )
        except (BrokenPipeError, OSError) as e:
            return f"<send failed to subagent {sid}: {e}>"
        return f"replied to {sid} (req={request_id})"

    return reply_to_subagent


def make_tell_subagent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the `tell_subagent` tool — registered on agents that
    can spawn subagents (`allow_meta=True`).

    Counterpart to `notify_parent` (issue #64) for the parent → child
    direction. Emits a `parent_note` event down the named subagent's
    pipe; the subagent's IO thread queues the formatted message onto
    its own `pending_async_replies` so the next LLM call sees it.
    Issue #65.
    """

    def tell_subagent(sid: str, text: str) -> str:
        """Send a non-blocking note down to a running subagent.

        Use this when you've learned something a still-running
        subagent should know — a new constraint, an upstream
        decision, an API that just changed. The subagent will see
        your note as a `[parent says]: <text>` user-role message
        at its next LLM-call boundary; it can act, defer, or
        ignore.

        Don't spam. One note should change the subagent's
        behaviour or understanding. If you have an actual question
        for the subagent, you don't have a tool for that — most
        useful patterns are: terminate + respawn with the new
        framing, or wait for the subagent to finish and brief it
        next round.

        Args:
            sid: The subagent id returned from `spawn_subagent`.
            text: The note text. Plain prose, self-contained.

        Returns:
            A short status string. Errors come back as `<...>`
            markers (empty text, unknown sid, dead subagent).
        """
        text = (text or "").strip()
        if not text:
            return "<refused: empty text>"
        sid = (sid or "").strip()
        if not sid:
            return "<refused: empty sid>"
        entry: SubagentEntry | None = agent._subagents.get(sid)
        if entry is None:
            return f"<unknown subagent {sid!r}>"
        if not entry.process.is_alive():
            return f"<subagent {sid} is no longer running>"
        try:
            protocol.send(entry.conn, "parent_note", text=text)
        except (BrokenPipeError, OSError) as e:
            return f"<send failed to subagent {sid}: {e}>"
        return f"sent to {sid}"

    return tell_subagent


def _collect_subagent_notes(
    agent: "Agent", sid: str, cursor: int | None
) -> dict[str, Any]:
    """Return a structured record of one subagent's notes.

    Data layer for peek_subagent — also intended to be reused by a
    future CLI `/notes` slash command, which would consume the same
    structured records via a query event (issue #65 comment).

    `cursor=None` means "no cursor; dump current ring, no
    missing-marker." `cursor=N` means "entries with seq > N; record
    `missing` for entries lost to overflow before the ring's
    earliest seq."

    Returned dict shape:
      {
        "sid": str,
        "name": str,
        "cursor": int,             # echoed back; 0 if input was None
        "next_cursor": int,        # seq to pass as `since` next time
        "missing": int,            # count of overflowed-past-cursor
        "entries": list[dict],     # {seq, ts, severity, text}
      }
    """
    entry = agent._subagents.get(sid)
    name = entry.name if entry is not None else sid
    with agent._notes_lock:
        ring = list(agent._subagent_notes.get(sid, ()))
        seq_next = agent._subagent_note_seq.get(sid, 0)

    if not ring:
        cur_label = 0 if cursor is None else cursor
        next_cur = max(cur_label, seq_next - 1) if seq_next > 0 else 0
        return {
            "sid": sid,
            "name": name,
            "cursor": cur_label,
            "next_cursor": next_cur,
            "missing": 0,
            "entries": [],
        }

    earliest_seq = ring[0][0]
    latest_seq = ring[-1][0]

    if cursor is None:
        cur_label = 0
        visible = ring
        missing = 0
    else:
        cur_label = cursor
        visible = [e for e in ring if e[0] > cursor]
        # Missing entries: seqs in (cursor, earliest_seq) were
        # dropped from the ring before this peek caught up.
        missing = max(0, earliest_seq - 1 - cursor)

    return {
        "sid": sid,
        "name": name,
        "cursor": cur_label,
        "next_cursor": latest_seq,
        "missing": missing,
        "earliest_seq": earliest_seq,
        "entries": [
            {"seq": s, "ts": ts, "severity": sev, "text": txt}
            for (s, ts, sev, txt) in visible
        ],
    }


def _format_peek_section(record: dict[str, Any]) -> str:
    """Render a structured note record (from `_collect_subagent_notes`)
    as the text-block one section of `peek_subagent`'s output.
    """
    name = record["name"]
    sid = record["sid"]
    cur = record["cursor"]
    next_cur = record["next_cursor"]
    missing = record["missing"]
    entries = record["entries"]
    header = f"[subagent {name} ({sid}) notes since cursor={cur}]:"
    lines = [header]
    if missing > 0:
        lines.append(
            f"  - (... {missing} note(s) before seq="
            f"{record.get('earliest_seq', '?')} were dropped from ring ...)"
        )
    if not entries:
        if missing == 0:
            lines.append(f"  - (no new notes; cursor={next_cur})")
        return "\n".join(lines)
    for e in entries:
        ts_label = f"t+{int(e['ts'])}s"
        lines.append(
            f"  - ({e['severity']}, {ts_label}) {e['text']}"
        )
    return "\n".join(lines)


def make_peek_subagent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the `peek_subagent` tool — registered on agents that
    can spawn subagents (`allow_meta=True`).

    Reads the per-sid notification ring (issue #64) without
    blocking. The model calls it between its own tool calls when
    it has reason to believe a sibling has news that would
    invalidate the next planned tool call. Issue #65.
    """

    def peek_subagent(
        sid: str | None = None, since: str | None = None
    ) -> str:
        """Do not call this reflexively.

        Default expectation: subagent notes surface as user-role
        messages at your next LLM-call boundary on their own. Peek
        is only for the case where *this turn's next tool call*
        depends on knowing — e.g., you're about to run a long
        test that a sibling subagent may have just made obsolete.
        Each peek costs a tool round-trip; routine "let me check"
        polling is waste.

        Args:
            sid: A specific subagent id, or `None` (default) to
                survey all live subagents.
            since: Cursor returned by a previous peek. Pass it
                back to skip notes you've already seen. Format:
                  - integer string (e.g. `"2"`) — only valid with
                    `sid`; means "I've seen up through seq 2".
                  - JSON object string (e.g. `'{"a-1234": 5}'`) —
                    multi-sid cursor; missing keys treated as 0.
                  - `None` — return everything currently in the
                    ring(s).

        Returns:
            Formatted text with one section per surveyed sid,
            optionally a `(... N notes dropped ...)` line when
            cursor falls below the ring's earliest seq, and a
            trailing `next_cursor: {...}` line (always JSON-shaped)
            you can pass back as `since` next time. Errors come
            back as `<...>` markers.
        """
        parsed_int: int | None = None
        parsed_dict: dict[str, int] | None = None
        if since is not None:
            since_str = since.strip()
            if since_str:
                try:
                    parsed_int = int(since_str)
                except ValueError:
                    try:
                        obj = json.loads(since_str)
                    except json.JSONDecodeError as e:
                        return f"<refused: invalid since: {e}>"
                    if not isinstance(obj, dict):
                        return (
                            "<refused: invalid since: must be int "
                            f"or JSON object, got {type(obj).__name__}>"
                        )
                    try:
                        parsed_dict = {
                            str(k): int(v) for k, v in obj.items()
                        }
                    except (ValueError, TypeError) as e:
                        return (
                            f"<refused: invalid since: cursor values "
                            f"must be ints ({e})>"
                        )

        if sid is not None:
            sid_clean = sid.strip()
            if not sid_clean:
                return "<refused: empty sid>"
            if sid_clean not in agent._subagents:
                return f"<unknown subagent {sid_clean!r}>"
            if parsed_int is not None:
                cursor: int | None = parsed_int
            elif parsed_dict is not None:
                cursor = parsed_dict.get(sid_clean, 0)
            else:
                cursor = None
            record = _collect_subagent_notes(agent, sid_clean, cursor)
            section = _format_peek_section(record)
            return (
                f"{section}\n"
                f"next_cursor: "
                f"{json.dumps({sid_clean: record['next_cursor']})}"
            )

        if parsed_int is not None:
            return (
                "<refused: integer since requires sid; use a JSON "
                "object cursor for multi-sid peek>"
            )
        sids = list(agent._subagents.keys())
        if not sids:
            return "<no live subagents>"
        sections: list[str] = []
        next_cursors: dict[str, int] = {}
        for s in sids:
            if parsed_dict is not None:
                cur: int | None = parsed_dict.get(s, 0)
            else:
                cur = None
            record = _collect_subagent_notes(agent, s, cur)
            sections.append(_format_peek_section(record))
            next_cursors[s] = record["next_cursor"]
        return (
            "\n\n".join(sections)
            + f"\n\nnext_cursor: {json.dumps(next_cursors)}"
        )

    return peek_subagent


def make_terminate_subagent(
    state: "_ChildState",
    agent: "Agent",
) -> Callable[..., str]:
    """Build the terminate_subagent tool."""

    def terminate_subagent(id: str) -> str:
        """Shut down a subagent and remove it from the registry.

        Sends a `shutdown` event over the pipe; if the process doesn't
        exit within a few seconds, escalates to SIGTERM and then SIGKILL.
        Idempotent — terminating an already-dead subagent is a no-op.

        Args:
            id: The subagent id returned from `spawn_subagent`.

        Returns:
            A short status string describing what happened.
        """
        entry: SubagentEntry | None = agent._subagents.pop(id, None)
        if entry is None:
            return f"<unknown subagent id: {id!r}>"

        state.unregister_subagent_pipe(id)
        # Drop the per-sid notification ring (issue #65). After
        # terminate, the sid is no longer peekable — late peeks
        # of dead sids should return the unknown-subagent marker.
        agent._clear_subagent_notes(id)

        if entry.process.is_alive():
            try:
                protocol.send(entry.conn, "shutdown")
            except (BrokenPipeError, OSError):
                pass
            entry.process.join(timeout=5)
            if entry.process.is_alive():
                try:
                    entry.process.terminate()  # SIGTERM
                except Exception:
                    pass
                entry.process.join(timeout=2)
            if entry.process.is_alive():
                # Last resort. terminate() should be enough, but a
                # subagent stuck in C extension code might not heed it.
                try:
                    import os
                    os.kill(entry.process.pid, signal.SIGKILL)
                except Exception:
                    pass
                entry.process.join(timeout=2)

        try:
            entry.conn.close()
        except Exception:
            pass

        state.send(
            "info",
            level="info",
            message=f"terminated subagent {entry.name} (id={id})",
        )
        return f"terminated {id} (exit_code={entry.process.exitcode})"

    return terminate_subagent
