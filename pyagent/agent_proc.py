"""Agent subprocess entrypoint.

The CLI process forks an agent into its own Python process via
`multiprocessing.spawn` and talks to it over a duplex Connection using
the event protocol in `pyagent.protocol`. An agent process can in turn
spawn its own subagents (Phase 3) — every subagent is just another
invocation of `child_main` with a slightly different config dict.

The child runs two threads:
  - **IO thread**: multiplexes incoming events from the upstream pipe
    (the CLI for the root, the parent agent for a subagent) AND from
    each child subagent's pipe. Classifies events onto queues and
    flags. Forwards subagent events upstream with an `agent_id` tag,
    so the CLI can label which subagent emitted what.
  - **Main thread**: pulls user_prompt events from a work queue and
    runs `agent.run`, with callbacks that emit assistant_text /
    tool_call_started / tool_result events back upstream.

Threads (not nested processes) inside the child because the agent loop
already serializes its own work — one prompt at a time — and the GIL
concern that motivated the parent/child split only matters for the
shared CLI loop. Within a single agent process, sequential tool
execution + a small IO thread is fine.
"""

from __future__ import annotations

import logging
import multiprocessing
import multiprocessing.connection
import os
import queue
import sys
import threading
import uuid
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from pyagent import paths
from pyagent import permissions
from pyagent import plugins as plugins_mod
from pyagent import protocol
from pyagent import roles as roles_mod
from pyagent import skills as skills_mod
from pyagent import subagent as subagent_mod
from pyagent import tools as agent_tools
from pyagent.agent import Agent
from pyagent.checklist import (
    Checklist,
    make_add_task,
    make_list_tasks,
    make_update_task,
)
from pyagent.llms import get_client
from pyagent.prompts import SystemPromptBuilder
from pyagent.session import Session

logger = logging.getLogger(__name__)


@dataclass
class _ChildState:
    conn: Connection
    work_queue: queue.Queue = field(default_factory=queue.Queue)
    permission_replies: queue.Queue = field(default_factory=queue.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    shutdown_event: threading.Event = field(default_factory=threading.Event)
    send_lock: threading.Lock = field(default_factory=threading.Lock)
    # Subagent fan-out: each entry has its own pipe (on `conn`) and a
    # reply queue the meta-tools block on. Mutated by spawn_subagent /
    # terminate_subagent via register_subagent_pipe / unregister_*.
    _subagent_conns: dict[str, Connection] = field(default_factory=dict)
    _subagent_reply_queues: dict[str, queue.Queue] = field(default_factory=dict)
    _subagent_lock: threading.Lock = field(default_factory=threading.Lock)
    # Recursion routing: any descendant id (a grandchild, a great-
    # grandchild, …) is mapped to the direct child whose subtree
    # contains it. Built lazily from any inbound event whose
    # `agent_id` differs from the direct-child sid it arrived on.
    # Used by `_handle_parent_event` to forward a downstream event
    # (e.g. permission_response) toward the right deep descendant.
    _descendants: dict[str, str] = field(default_factory=dict)
    # Set after _bootstrap so the IO thread can look up SubagentEntry
    # status (sync vs async mode) when routing turn_complete events.
    # Optional because some unit tests construct a _ChildState
    # without an Agent.
    agent: Any = None
    # Tag every outbound event with our own agent_id when forwarding
    # subagent events upstream. None = root (no annotation needed).
    self_agent_id: str | None = None
    # ask_parent / reply_to_subagent (issue #47):
    #   - As subagent: each outstanding `ask_parent` call registers
    #     a Queue here keyed by request_id. The IO thread routes the
    #     parent's `parent_answer` event to the matching queue.
    #   - As parent: each inbound `subagent_ask` from a direct child
    #     records request_id -> sid here so `reply_to_subagent` knows
    #     which child to send the answer to.
    # Two distinct uses of "request_id keyed", lifetimes don't
    # collide because asks-out and asks-in are separate populations.
    _pending_ask_replies: dict[str, queue.Queue] = field(default_factory=dict)
    _inbound_ask_sid: dict[str, str] = field(default_factory=dict)
    _ask_lock: threading.Lock = field(default_factory=threading.Lock)
    # permission_handler / permission_response routing (issue #69):
    # each outstanding permission prompt registers a Queue here keyed
    # by request_id; the IO thread routes the matching response to it.
    # Replaces the single shared `permission_replies` queue for the
    # case where multiple concurrent permission prompts are
    # in-flight (e.g. parallel subagents). The shared queue stays
    # for backward-compatibility callers but isn't used by the new
    # protocol.
    _pending_perm_replies: dict[str, queue.Queue] = field(default_factory=dict)
    _perm_lock: threading.Lock = field(default_factory=threading.Lock)
    # Set while the agent's main thread is inside _run_turn so the IO
    # thread can decide whether a `user_note` should land on the
    # mid-turn inbox (turn active) or be promoted to a fresh
    # `user_prompt` (idle window). Issue #68.
    turn_active: threading.Event = field(default_factory=threading.Event)
    # Highest context-utilization tier we've already warned about
    # this session. Drives "warn once per crossing" behavior so the
    # 80% chat warning doesn't repeat on every turn that stays above
    # the line. Reset to -1 (no tier yet) at construction; tiers are
    # the integers 0/1/2 corresponding to the 60/80/95% thresholds.
    _context_warn_tier: int = -1

    def send(self, event_type: str, **payload: Any) -> None:
        """Send a typed event upstream (CLI for root, parent agent for sub).

        Concurrent threads (main + permission handler + IO thread
        forwarding) all emit outbound events; serialize so one event's
        bytes don't interleave with another's.
        """
        with self.send_lock:
            try:
                protocol.send(self.conn, event_type, **payload)
            except (BrokenPipeError, OSError):
                self.shutdown_event.set()

    def _send_dict(self, event: dict) -> None:
        """Send a pre-built event dict (used when forwarding upstream)."""
        with self.send_lock:
            try:
                self.conn.send(event)
            except (BrokenPipeError, OSError):
                self.shutdown_event.set()

    def register_subagent_pipe(
        self, sid: str, conn: Connection
    ) -> queue.Queue:
        """Hook a new subagent's pipe into the IO loop's multiplex.

        Called from `spawn_subagent` after the subprocess starts. The
        IO thread picks up the new connection on its next iteration
        (within ~100ms). Returns the per-subagent reply queue that
        spawn_subagent waits on for `ready` and call_subagent waits on
        for `turn_complete`.
        """
        rq: queue.Queue = queue.Queue()
        with self._subagent_lock:
            self._subagent_conns[sid] = conn
            self._subagent_reply_queues[sid] = rq
        return rq

    def unregister_subagent_pipe(self, sid: str) -> None:
        """Remove a subagent from the multiplex set. Called from
        terminate_subagent or when a subagent's pipe sees EOF.

        Sweeps the descendants table too — every descendant whose
        path went through `sid` is now unreachable, since the whole
        subtree dies with the direct child.
        """
        with self._subagent_lock:
            self._subagent_conns.pop(sid, None)
            self._subagent_reply_queues.pop(sid, None)
            stale = [
                d for d, via in self._descendants.items() if via == sid
            ]
            for d in stale:
                self._descendants.pop(d, None)

    def _snapshot_conns(self) -> tuple[list[Connection], dict[int, str]]:
        """Snapshot current set of (parent + subagent) conns for wait().

        Returns a list of conns and a fileno→sid mapping so the IO
        loop can identify which subagent emitted an event.
        """
        with self._subagent_lock:
            sub_items = list(self._subagent_conns.items())
        conns: list[Connection] = [self.conn]
        fileno_to_sid: dict[int, str] = {}
        for sid, c in sub_items:
            conns.append(c)
            fileno_to_sid[c.fileno()] = sid
        return conns, fileno_to_sid

    def io_loop(self) -> None:
        while not self.shutdown_event.is_set():
            conns, fileno_to_sid = self._snapshot_conns()
            try:
                ready = multiprocessing.connection.wait(conns, timeout=0.1)
            except OSError:
                # A conn closed underneath us — re-snapshot and retry.
                continue
            for c in ready:
                if c is self.conn:
                    self._handle_parent_event()
                else:
                    sid = fileno_to_sid.get(c.fileno())
                    if sid is None:
                        # Subagent was deregistered between snapshot and
                        # wait — drain and drop.
                        try:
                            c.recv()
                        except (EOFError, OSError):
                            pass
                        continue
                    self._handle_subagent_event(sid, c)

    def _handle_parent_event(self) -> None:
        try:
            event = self.conn.recv()
        except (EOFError, OSError):
            self.shutdown_event.set()
            return
        kind = event.get("type")
        target_sid = event.get("agent_id")  # None means "for me"
        if target_sid:
            with self._subagent_lock:
                direct_conn = self._subagent_conns.get(target_sid)
                via_child = (
                    None if direct_conn is not None
                    else self._descendants.get(target_sid)
                )
                via_conn = (
                    self._subagent_conns.get(via_child)
                    if via_child is not None
                    else None
                )
            if direct_conn is not None:
                # Target is OUR direct child — strip agent_id so the
                # subagent sees a normal "for me" event.
                forwarded = dict(event)
                forwarded.pop("agent_id", None)
                try:
                    direct_conn.send(forwarded)
                except (BrokenPipeError, OSError):
                    logger.warning(
                        "forward to subagent %r failed; dropping event",
                        target_sid,
                    )
                return
            if via_conn is not None:
                # Target is a deeper descendant. Pass the event down
                # the chain unchanged — the deeper hop where it lands
                # on a direct-child match will do the strip.
                try:
                    via_conn.send(event)
                except (BrokenPipeError, OSError):
                    logger.warning(
                        "forward toward descendant %r via %r failed",
                        target_sid,
                        via_child,
                    )
                return
            logger.warning(
                "drop event for unknown subagent %r: %r", target_sid, kind
            )
            return

        if kind == "user_prompt":
            self.work_queue.put(event)
        elif kind == "user_note":
            # Issue #68: mid-turn typed input from the human. While
            # a turn is running, queue as [user adds]: <text> onto
            # the agent's inbox so the next LLM call sees it
            # mid-turn. While idle (the brief gap between
            # turn_complete arriving at the CLI and the user typing
            # again), promote to a fresh user_prompt so the agent
            # actually responds — otherwise a stray idle-window
            # note would sit in the inbox until the next prompt
            # arrives, surprising the user.
            text = (event.get("text", "") or "").strip()
            if not text:
                logger.debug("user_note with empty text; dropping")
                return
            if self.turn_active.is_set():
                if self.agent is not None:
                    self.agent.pending_async_replies.put(
                        f"[user adds]: {text}"
                    )
            else:
                # Idle promotion: kick off a turn whose initial
                # user message is the note text.
                self.work_queue.put(
                    {"type": "user_prompt", "prompt": text}
                )
        elif kind == "cancel":
            self.cancel_event.set()
            # SIGKILL any in-flight execute() shell so the foreground
            # tool returns control to the agent loop in time for the
            # next safe-point cancel check.
            try:
                killed = agent_tools.kill_active()
                if killed:
                    logger.info(
                        "cancel: killed %d active shell process(es)", killed
                    )
            except Exception:
                logger.exception("cancel: error killing active shell")
            # Propagate cancel down to all subagents.
            with self._subagent_lock:
                conns = list(self._subagent_conns.values())
            for c in conns:
                try:
                    c.send({"type": "cancel"})
                except (BrokenPipeError, OSError):
                    pass
        elif kind == "permission_response":
            # Issue #69: route by request_id to the matching
            # per-request queue. Falls back to the shared
            # permission_replies queue if no request_id is
            # present (defensive — shouldn't happen with the new
            # CLI but keeps stale callers from silently hanging).
            req_id = event.get("request_id", "")
            if req_id:
                with self._perm_lock:
                    rq = self._pending_perm_replies.pop(req_id, None)
                if rq is not None:
                    rq.put(event)
                else:
                    logger.warning(
                        "permission_response for unknown request_id %r; "
                        "dropping",
                        req_id,
                    )
            else:
                self.permission_replies.put(event)
        elif kind == "parent_answer":
            # Reply to a prior `ask_parent` from this subagent. Route
            # to the matching request_id queue so the blocked tool
            # call can return. Issue #47.
            req_id = event.get("request_id", "")
            with self._ask_lock:
                rq = self._pending_ask_replies.pop(req_id, None)
            if rq is not None:
                rq.put(event.get("answer", "") or "")
            else:
                logger.warning(
                    "parent_answer for unknown request_id %r; dropping",
                    req_id,
                )
        elif kind == "parent_note":
            # Non-blocking note from this agent's parent. Queue as a
            # formatted user-role message onto our own
            # pending_async_replies so the next LLM call sees it.
            # Issue #64. No producer ships in this PR; #65 adds
            # `tell_subagent` which fires this event downward.
            text = event.get("text", "") or ""
            if self.agent is not None:
                self.agent.pending_async_replies.put(
                    f"[parent says]: {text}"
                )
        elif kind == "set_model":
            self._handle_set_model(event.get("model", ""))
        elif kind == "shutdown":
            self.shutdown_event.set()
            self.work_queue.put({"type": "shutdown"})
        else:
            logger.warning("child: unknown event type %r", kind)

    def _handle_set_model(self, model: str) -> None:
        """Swap the agent's LLM client to a new model.

        Mid-turn swaps are tolerated — `_call_llm` reads `agent.client`
        fresh on each turn, so the next API call uses the new client.
        Construction failures (unknown provider, missing API key) are
        surfaced as `info` events; the existing client stays in place.
        """
        if self.agent is None or not model:
            return
        try:
            new_client = get_client(model)
        except Exception as e:
            self.send(
                "info",
                level="warn",
                message=(
                    f"set_model {model!r} failed: "
                    f"{type(e).__name__}: {e}"
                ),
            )
            return
        self.agent.client = new_client
        self.send(
            "info", level="info", message=f"model swapped to {model}"
        )

    def _handle_subagent_event(self, sid: str, conn: Connection) -> None:
        try:
            event = conn.recv()
        except (EOFError, OSError):
            # Pipe closed. If terminate_subagent already removed the
            # sid from our registry, this is the expected fallout of
            # that termination — stay quiet. If the sid is still
            # tracked, the subagent crashed or exited on its own and
            # the human deserves a warning.
            with self._subagent_lock:
                unexpected = sid in self._subagent_conns
            self.unregister_subagent_pipe(sid)
            # Drop the per-sid notification ring (issue #65). The
            # sid is gone for good — peek_subagent of it should
            # return <unknown subagent ...> on the next call.
            if self.agent is not None:
                self.agent._clear_subagent_notes(sid)
            if unexpected:
                self._send_dict(
                    {
                        "type": "info",
                        "level": "warn",
                        "message": f"subagent {sid} closed its pipe unexpectedly",
                        "agent_id": sid,
                    }
                )
            return
        kind = event.get("type")
        inner_id = event.get("agent_id")
        # ready / turn_complete / agent_error → reply queue, but ONLY
        # when the event originated from THIS direct child (no
        # agent_id annotation). If the event has agent_id set, it
        # bubbled up from a grandchild via direct child `sid`; it
        # would land on the wrong reply queue and confuse the
        # waiting spawn/call here.
        if kind in ("ready", "turn_complete", "agent_error"):
            if inner_id is None:
                # Async-fired call? If so, route the final_text into
                # the parent agent's pending_async_replies inbox
                # instead of the per-sid sync reply queue. The
                # waiting call_subagent_async (if it still cared,
                # which it doesn't) would not be blocked on the
                # reply queue, so this routing is purely about
                # delivering the reply to the LLM via the next
                # turn's drain.
                routed_async = False
                if kind == "turn_complete" and self.agent is not None:
                    entry = self.agent._subagents.get(sid)
                    if entry is not None and getattr(entry, "mode", None) == "async":
                        text = event.get("final_text", "") or ""
                        formatted = (
                            f"[subagent {entry.name} ({sid}) reports]: {text}"
                        )
                        self.agent.pending_async_replies.put(formatted)
                        entry.mode = None
                        routed_async = True
                if not routed_async:
                    with self._subagent_lock:
                        rq = self._subagent_reply_queues.get(sid)
                    if rq is not None:
                        rq.put(event)
            else:
                # Bubbled up from a deeper descendant. Remember the
                # route so a downward event (e.g. permission_response)
                # bound for `inner_id` can find its way back through
                # direct child `sid`.
                with self._subagent_lock:
                    self._descendants[inner_id] = sid
            # agent_error and ready are also worth surfacing upstream
            # so the human can see them; turn_complete is consumed
            # locally by the waiting call_subagent (or routed to the
            # async inbox above) and shouldn't pile up in the CLI's
            # event stream.
            if kind != "turn_complete":
                self._forward_upstream(event, sid)
            return
        if kind == "subagent_ask":
            # A direct child is asking THIS agent a question
            # mid-turn. Consume locally — record the request_id ->
            # sid mapping so `reply_to_subagent` can find the
            # waiting child, and inject the formatted question into
            # the parent's `pending_async_replies` so it shows up
            # as a user-role message at the start of the next turn.
            # Issue #47.
            #
            # If `inner_id` is set, the event bubbled up from a
            # grandchild via direct child `sid` — that's a bug,
            # since `ask_parent` always targets the IMMEDIATE
            # parent. Drop with a warning instead of forwarding
            # to avoid leaking "wrong addressee" routing.
            if inner_id is not None:
                logger.warning(
                    "subagent_ask bubbled past its direct parent "
                    "(inner_id=%r via sid=%r); dropping",
                    inner_id, sid,
                )
                # Surface to the CLI so the human can see something
                # went sideways without a silent drop.
                self._forward_upstream(event, sid)
                return
            req_id = event.get("request_id", "")
            question = event.get("question", "") or ""
            with self._ask_lock:
                self._inbound_ask_sid[req_id] = sid
            if self.agent is not None:
                entry = self.agent._subagents.get(sid)
                name = entry.name if entry else sid
                formatted = (
                    f"[subagent {name} ({sid}) asks (req={req_id})]: "
                    f"{question}"
                )
                self.agent.pending_async_replies.put(formatted)
            # Surface upstream so the CLI can render the cross-agent
            # conversation in the transcript. _forward_upstream stamps
            # `agent_id=sid` so the CLI knows which subagent originated.
            self._forward_upstream(event, sid)
            return
        if kind == "subagent_note":
            # A direct child dropped a non-blocking note. Append to
            # the per-sid ring on the parent agent and queue a
            # formatted user-role message onto pending_async_replies
            # so the parent's next LLM call sees it. Issue #64.
            #
            # Notes always target the IMMEDIATE parent — a bubbled
            # variant from a grandchild is dropped with a warning,
            # mirroring the subagent_ask rule above.
            if inner_id is not None:
                logger.warning(
                    "subagent_note bubbled past its direct parent "
                    "(inner_id=%r via sid=%r); dropping",
                    inner_id, sid,
                )
                self._forward_upstream(event, sid)
                return
            severity = event.get("severity", "info") or "info"
            text = event.get("text", "") or ""
            if self.agent is not None:
                self.agent._append_subagent_note(sid, severity, text)
                entry = self.agent._subagents.get(sid)
                name = entry.name if entry else sid
                formatted = (
                    f"[subagent {name} ({sid}) notes ({severity})]: "
                    f"{text}"
                )
                self.agent.pending_async_replies.put(formatted)
            # Surface upstream so the CLI can render the note in
            # the transcript like any other cross-agent event.
            self._forward_upstream(event, sid)
            return
        # Everything else (assistant_text, tool_*, info, permission_request).
        # Learn the route if this event came from a deeper descendant,
        # then forward upstream so the CLI can render it.
        if inner_id is not None and inner_id != sid:
            with self._subagent_lock:
                self._descendants[inner_id] = sid
        self._forward_upstream(event, sid)

    def _forward_upstream(self, event: dict, sid: str) -> None:
        out = dict(event)
        # Preserve any deeper agent_id chain if present (future
        # recursion); otherwise stamp this subagent's id.
        out.setdefault("agent_id", sid)
        self._send_dict(out)

    def permission_handler(self, target: Path) -> bool:
        # Issue #69: per-request reply queue keyed by request_id so
        # multiple concurrent permission prompts (e.g. from parallel
        # subagents) don't collide on a single shared queue. The CLI
        # echoes request_id back on permission_response; the IO
        # thread routes by id to the matching queue here.
        req_id = f"perm-{uuid.uuid4().hex[:8]}"
        rq: queue.Queue = queue.Queue(maxsize=1)
        with self._perm_lock:
            self._pending_perm_replies[req_id] = rq
        try:
            self.send(
                "permission_request",
                request_id=req_id,
                target=str(target),
            )
            try:
                reply = rq.get(timeout=300)
            except queue.Empty:
                logger.warning(
                    "permission prompt timed out for %s (req=%s)",
                    target, req_id,
                )
                return False
        finally:
            with self._perm_lock:
                self._pending_perm_replies.pop(req_id, None)
        if reply.get("always"):
            permissions.pre_approve(target)
        return bool(reply.get("decision"))


def _register_tools(
    agent: Agent,
    *,
    state: "_ChildState | None" = None,
    parent_session: Session | None = None,
    base_config: dict[str, Any] | None = None,
    allow_meta: bool = False,
    allowlist: list[str] | None = None,
    checklist: Checklist | None = None,
) -> None:
    """Register the default tool set on `agent`.

    `allow_meta=True` registers spawn_subagent / call_subagent /
    terminate_subagent on top of the default set — used for root
    agents and for subagents whose role permits further spawning.

    `allowlist` (when non-None) restricts registration to only the
    named tools — used for role-scoped subagents like a read-only
    validator. Names not in the default set are silently ignored.
    The allowlist *cannot* add tools that don't exist; it can only
    narrow.
    """
    def _add(name: str, fn: Any, **kw: Any) -> None:
        if allowlist is None or name in allowlist:
            agent.add_tool(name, fn, **kw)

    _add("read_file", agent_tools.read_file, auto_offload=False)
    _add("write_file", agent_tools.write_file)
    _add("edit_file", agent_tools.edit_file)
    _add("list_directory", agent_tools.list_directory)
    _add("grep", agent_tools.grep)
    _add("glob", agent_tools.glob, auto_offload=False)
    _add("execute", agent_tools.execute)
    # Long-running shell. `read_output` can return a lot of bytes —
    # let auto_offload move oversized reads to attachments. The other
    # three return short status strings; offloading them just adds
    # noise to the conversation.
    _add("run_background", agent_tools.run_background, auto_offload=False)
    _add("read_output", agent_tools.read_output)
    _add("wait_for", agent_tools.wait_for, auto_offload=False)
    _add("kill_process", agent_tools.kill_process, auto_offload=False)
    _add("fetch_url", agent_tools.fetch_url)
    # read_ledger / write_ledger are now provided by the bundled
    # memory-markdown plugin (see pyagent/plugins/memory_markdown/).
    # Disabling that plugin removes the tools entirely — clean
    # replacement surface for alternative memory backends.
    # Skill bodies are single-shot reference content: the model reads
    # one to decide what to do next, the next assistant turn records
    # that decision, after which the body is dead weight on every
    # subsequent turn. `evict_after_use=True` swaps the result for a
    # short stub once the consuming assistant turn has produced
    # output. Recovery is a second `read_skill` call. Issue #10.
    _add(
        "read_skill",
        skills_mod.read_skill,
        auto_offload=False,
        evict_after_use=True,
    )
    if checklist is not None:
        # Checklist tools share state with the CLI footer via a
        # per-mutation `checklist` event. Roles can scope these out via
        # the allowlist (e.g. a one-shot validator subagent shouldn't
        # be maintaining a task list).
        _add("add_task", make_add_task(checklist), auto_offload=False)
        _add("update_task", make_update_task(checklist), auto_offload=False)
        _add("list_tasks", make_list_tasks(checklist), auto_offload=False)
    # ask_parent (issue #47): only meaningful for subagents — the
    # root has no parent above. Gate on `state.self_agent_id` being
    # set (which `_bootstrap` does for any `is_subagent=True` config).
    if state is not None and state.self_agent_id is not None:
        _add(
            "ask_parent",
            subagent_mod.make_ask_parent(state, agent),
            auto_offload=False,
        )
        # notify_parent (issue #64): non-blocking, fire-and-forget.
        # Counterpart to ask_parent for cases where the subagent
        # has information for the parent but doesn't need a reply.
        _add(
            "notify_parent",
            subagent_mod.make_notify_parent(state, agent),
            auto_offload=False,
        )
    # pip_install (issue #46): registered ONLY on the root agent.
    # The root owns the workspace's `.venv/` and serializes installs
    # via its single-threaded turn loop. Subagents don't get this
    # tool — they `ask_parent("install <spec>")` and the root's LLM
    # decides whether to act. That's the parent-as-broker pattern
    # that #47 unlocked, replacing the flock approach the issue
    # originally proposed.
    if state is not None and state.self_agent_id is None:
        # `base_config["cwd"]` is the workspace path; `_bootstrap`
        # always sets it. Tools registered without `base_config` are
        # the test-only path — skip pip_install there.
        if base_config is not None:
            _add(
                "pip_install",
                agent_tools.make_pip_install(Path(base_config["cwd"])),
                auto_offload=False,
            )
    if allow_meta:
        assert state is not None and parent_session is not None and base_config is not None
        _add(
            "spawn_subagent",
            subagent_mod.make_spawn_subagent(
                state, agent, parent_session, base_config
            ),
        )
        _add(
            "call_subagent",
            subagent_mod.make_call_subagent(state, agent),
        )
        _add(
            "call_subagent_async",
            subagent_mod.make_call_subagent_async(state, agent),
        )
        _add(
            "wait_for_subagents",
            subagent_mod.make_wait_for_subagents(state, agent),
        )
        _add(
            "terminate_subagent",
            subagent_mod.make_terminate_subagent(state, agent),
        )
        # reply_to_subagent (issue #47): the counterpart to
        # ask_parent. Only meaningful when this agent has children
        # to reply to, hence gated on allow_meta alongside the
        # spawn family.
        _add(
            "reply_to_subagent",
            subagent_mod.make_reply_to_subagent(state, agent),
            auto_offload=False,
        )
        # tell_subagent / peek_subagent (issue #65): non-blocking
        # parent → child push and parent-side read of the per-sid
        # notification ring. Both are pure tool factories on the
        # protocol + storage shipped in #64.
        _add(
            "tell_subagent",
            subagent_mod.make_tell_subagent(state, agent),
            auto_offload=False,
        )
        _add(
            "peek_subagent",
            subagent_mod.make_peek_subagent(state, agent),
            auto_offload=False,
        )


def _bootstrap(
    config: dict[str, Any], state: _ChildState
) -> tuple[Agent, Session, plugins_mod.LoadedPlugins]:
    """Replicate the CLI's startup setup inside the child process.

    Handles both root agents and subagents. Subagents have
    `is_subagent=True` in the config and use a custom session_root.
    Both build a `SystemPromptBuilder`; the subagent path additionally
    layers a `role_body` (from the spawn-time role definition) and a
    `task_body` (from the spawn-time `system_prompt` argument) on top
    of the universal SOUL/TOOLS/PRIMER base.
    """
    # Close stdin so a buggy library or tool that calls input() can't
    # steal raw keystrokes from the CLI's prompt_toolkit input field
    # (which holds the controlling tty in raw mode).
    try:
        sys.stdin.close()
    except OSError:
        pass
    sys.stdin = open(os.devnull, "r")

    os.chdir(config["cwd"])
    permissions.set_workspace(config["cwd"])
    permissions.pre_approve(paths.config_dir())
    for p in config.get("approved_paths", []):
        permissions.pre_approve(p)
    permissions.set_prompt_handler(state.permission_handler)

    is_subagent = bool(config.get("is_subagent"))
    state.self_agent_id = config["session_id"] if is_subagent else None

    # Plugin loading. is_subagent is True for spawned subagents — the
    # plugins module honors `[load] in_subagents = false` to skip
    # plugins that aren't parallel-safe.
    #
    # Must run BEFORE `get_client`: plugins can register LLM providers
    # (via `api.register_provider`) that the loader publishes to
    # `pyagent.llms`. Loading first means `--model <plugin-provider>/foo`
    # resolves at bootstrap; if we called get_client first the plugin
    # providers wouldn't be visible yet.
    loaded_plugins = plugins_mod.load(is_subagent=is_subagent)

    client = get_client(config["model"])

    # role_meta_tools defaults True so non-role spawns and root agents
    # keep the existing fan-out behavior. Roles can disable meta-tools
    # to mark a subagent as a leaf (validator, summarizer, etc.).
    allow_meta = bool(config.get("role_meta_tools", True))
    # role_tools is the allowlist; None means inherit the default set.
    allowlist = config.get("role_tools")
    # Leaf subagents skip the role catalog — showing roles they can't
    # spawn is misleading prose.
    catalog_for_roles = roles_mod.catalog if allow_meta else ""

    if is_subagent:
        session = Session(
            session_id=config["session_id"],
            root=Path(config["session_root"]),
        )
        # Plant a small breadcrumb so the on-disk tree is self-describing.
        session.dir.mkdir(parents=True, exist_ok=True)
        try:
            (session.dir / "parent.txt").write_text(
                f"parent_session_id: {config.get('parent_session_id', '')}\n"
                f"depth: {config.get('depth', 0)}\n"
            )
        except OSError:
            pass
        system: SystemPromptBuilder = SystemPromptBuilder(
            soul=Path(config["soul_path"]),
            tools=Path(config["tools_path"]),
            primer=Path(config["primer_path"]),
            skills_catalog=skills_mod.live_catalog,
            roles_catalog=catalog_for_roles,
            role_body=config.get("role_body", ""),
            task_body=config.get("task_body", ""),
            plugin_loader=loaded_plugins,
        )
    else:
        session = Session(session_id=config["session_id"])
        system = SystemPromptBuilder(
            soul=Path(config["soul_path"]),
            tools=Path(config["tools_path"]),
            primer=Path(config["primer_path"]),
            skills_catalog=skills_mod.live_catalog,
            roles_catalog=catalog_for_roles,
            plugin_loader=loaded_plugins,
        )

    # Now that the session exists, expose it to plugins so
    # PluginAPI.write_session_attachment can resolve a real path.
    # Bench / no-session contexts skip this step → plugins fall back
    # to inline-only rendering.
    loaded_plugins.bind_session(session)

    agent = Agent(
        client=client,
        system=system,
        session=session,
        depth=int(config.get("depth", 0)),
        plugins=loaded_plugins,
    )
    # Hand the IO thread a reference so it can look up SubagentEntry
    # status (sync vs async mode) when routing turn_complete events.
    state.agent = agent

    # notes_unread event (issue #65 comment, feeds #67 footer): only
    # fire from the root agent. Deeper notes don't bubble per the
    # _handle_subagent_event rule, so subagent rings are local-only
    # and don't need a counter visible to the CLI.
    if not is_subagent:
        def _emit_notes_unread(count: int, by_severity: dict[str, int]) -> None:
            state.send(
                "notes_unread", count=count, by_severity=by_severity
            )
        agent._notes_unread_emitter = _emit_notes_unread

    # Checklist tools live on the root agent only — a per-session
    # construct, not per-agent. Subagents that try to track their
    # own work would compete with the root's list for the user's
    # one footer slot, and subagent runs are typically too short to
    # warrant a checklist anyway.
    if not is_subagent:
        checklist = Checklist(
            session.dir / "checklist.json",
            on_change=lambda tasks: state.send("checklist", tasks=tasks),
        )
        # Replay the persisted snapshot to the CLI on resume so the
        # footer reflects prior state immediately, before the model
        # touches the list.
        if checklist.tasks:
            state.send("checklist", tasks=checklist.list())
    else:
        checklist = None

    # Meta-tools registered when the role allows further spawning.
    # Recursion is bounded by `max_depth` in config (the spawn tool
    # refuses if `agent.depth + 1 > max_depth`). Roles with
    # `meta_tools = false` mark a subagent as a leaf — no spawn /
    # call / terminate registered. Tool allowlist further narrows
    # the default set when a role has `tools = [...]`.
    _register_tools(
        agent,
        state=state,
        parent_session=session,
        base_config=config,
        allow_meta=allow_meta,
        allowlist=allowlist,
        checklist=checklist,
    )

    # Plugin introspection tool, available to the agent for self-
    # improvement workflows.
    agent.add_tool(
        "list_plugins", plugins_mod.make_list_plugins_tool(loaded_plugins)
    )

    # Register plugin tools. Built-ins win on conflict — a plugin that
    # tries to claim a built-in tool name is logged and skipped.
    builtin_names = set(agent.tools.keys())
    for tool_name, (plugin_name, fn) in loaded_plugins.tools().items():
        if tool_name in builtin_names:
            logger.warning(
                "plugin %s: tool %r conflicts with a built-in; skipping",
                plugin_name,
                tool_name,
            )
            continue
        agent.add_tool(tool_name, fn)

    agent.conversation = session.load_history()
    if agent.conversation:
        # JSONL on disk keeps full skill-body content (round-trip
        # invariant — see smoke_session_replay). On resume, apply the
        # same eviction pass that runs after each live assistant turn
        # so in-memory state matches what would have been there had
        # the session run continuously. Issue #10.
        agent._apply_eviction()
        orphans = session.find_orphan_attachments()
        if orphans:
            session.purge_orphan_attachments(orphans=orphans)
            state.send(
                "info",
                level="info",
                message=f"purged {len(orphans)} orphan attachment(s)",
            )
    return agent, session, loaded_plugins


# Context-utilization warning thresholds. List of (percent, label,
# message_template). Tier index in this list IS the integer stored
# in `_ChildState._context_warn_tier`; new entries should be added
# in ascending percent order. Crossing a tier emits an `info` event
# to the chat once; the per-turn `context_status` event still flows
# unconditionally so the footer always reflects current state.
_CONTEXT_WARN_TIERS = (
    (60, "info", "context: {pct}% of {window:,} tokens used"),
    (
        80,
        "warn",
        "context: {pct}% of {window:,} tokens used — approaching the limit",
    ),
    (
        95,
        "warn",
        "context: {pct}% of {window:,} tokens used — next turn likely to fail",
    ),
)


def _emit_context_status(
    state: _ChildState, agent: Agent
) -> None:
    """After each turn, compute context utilization vs the model's
    window and emit one `context_status` event for the footer plus,
    on a tier crossing, one `info` event for the chat.

    Token counting strategy: we use the *previous turn's*
    ``usage.input`` as a stand-in for "current context size."
    That's what the provider just paid attention to; the next
    turn's input will be roughly that plus output plus any new
    user/tool messages. It under-counts the not-yet-sent next
    prompt slightly, but over-warns rather than missing the limit.

    Window=0 means the client doesn't know its own context size
    (older Ollama, pyagent stubs); skip the emission entirely so the
    footer hides the segment instead of showing a useless 0%.
    """
    client = getattr(agent, "client", None)
    if client is None:
        return
    window = int(getattr(client, "context_window", 0) or 0)
    if window <= 0:
        return
    used = int(agent.token_usage.get("input", 0) or 0)
    if used <= 0:
        return
    pct = max(0, min(100, int(used * 100 / window)))
    state.send("context_status", pct=pct, used=used, window=window)

    # Emit a chat info on the highest tier we've crossed but not yet
    # warned about. We track the highest tier reached, not just the
    # immediate crossing, so a turn that jumps multiple tiers at once
    # (e.g. 50% → 90%) still surfaces the most-severe warning.
    new_tier = -1
    for i, (threshold, _level, _msg) in enumerate(_CONTEXT_WARN_TIERS):
        if pct >= threshold:
            new_tier = i
    if new_tier > state._context_warn_tier:
        state._context_warn_tier = new_tier
        threshold, level, template = _CONTEXT_WARN_TIERS[new_tier]
        state.send(
            "info",
            level=level,
            message=template.format(pct=pct, window=window),
        )


def _run_turn(
    state: _ChildState,
    agent: Agent,
    session: Session,
    prompt: str,
    persist: bool,
) -> None:
    saved = len(agent.conversation)
    state.cancel_event.clear()
    final_text = ""
    try:
        final_text = agent.run(
            prompt,
            on_text=lambda t: state.send("assistant_text", text=t),
            # Streaming text deltas — fire as the provider produces
            # them. The CLI accumulates and renders incrementally;
            # the trailing `assistant_text` event still carries the
            # full, completed text so non-streaming consumers (and
            # the markdown re-render at end-of-turn) work uniformly.
            on_text_delta=lambda t: state.send("assistant_text_delta", text=t),
            on_tool_call=lambda n, a: state.send(
                "tool_call_started", name=n, args=a
            ),
            on_tool_result=lambda n, c: state.send(
                "tool_result", name=n, content=c
            ),
            on_usage=lambda u: (
                state.send(
                    "usage",
                    input=int(u.get("input", 0) or 0),
                    output=int(u.get("output", 0) or 0),
                    cache_creation=int(u.get("cache_creation", 0) or 0),
                    cache_read=int(u.get("cache_read", 0) or 0),
                ),
                # Right after the per-LLM-call usage update is
                # forwarded, recompute context utilization. Doing it
                # here (rather than at turn_complete) means the
                # footer gauge updates between tool batches in a
                # multi-call turn, matching the rest of the gutter
                # which is already mid-turn-aware.
                _emit_context_status(state, agent),
            ),
            cancel_event=state.cancel_event,
        )
    except KeyboardInterrupt:
        del agent.conversation[saved:]
        state.send(
            "agent_error",
            kind="KeyboardInterrupt",
            message="interrupted",
            fatal=False,
        )
        return
    except Exception as e:
        logger.exception("agent.run raised")
        del agent.conversation[saved:]
        state.send(
            "agent_error",
            kind=type(e).__name__,
            message=str(e),
            fatal=False,
        )
        return

    if persist:
        session.append_history(agent.conversation[saved:])
    else:
        # Memory-pass turn or other transient: ledger writes already
        # on disk via tool calls; the canned exchange must NOT enter
        # the saved transcript or it'd masquerade as a real turn on
        # resume.
        del agent.conversation[saved:]
    state.send("turn_complete", final_text=final_text)


def _terminate_subagents(state: _ChildState, agent: Agent) -> None:
    """Best-effort shutdown of all live subagents on this agent's exit.

    Used both at clean shutdown and when a fatal bootstrap error tears
    the process down. Mirrors `terminate_subagent` but doesn't bother
    with the registry-removal niceties (process is exiting).
    """
    with state._subagent_lock:
        ids = list(state._subagent_conns.keys())
    for sid in ids:
        entry = agent._subagents.get(sid)
        if entry is None:
            continue
        try:
            protocol.send(entry.conn, "shutdown")
        except (BrokenPipeError, OSError):
            pass
        entry.process.join(timeout=3)
        if entry.process.is_alive():
            try:
                entry.process.terminate()
            except Exception:
                pass
            entry.process.join(timeout=2)


def _set_parent_death_signal() -> None:
    """Best-effort: ask the kernel to SIGTERM us if the parent dies.

    Linux-only. Crash-safety belt for the case where the CLI process
    is SIGKILLed (or segfaults) and never gets to run its try/finally
    cleanup. With daemon=False on the agent process, no automatic
    cleanup happens on parent death — this hook is what closes the
    gap. No-op on platforms that lack `prctl`.
    """
    try:
        import ctypes
        import signal as _signal

        # PR_SET_PDEATHSIG = 1 (from <linux/prctl.h>)
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            1, _signal.SIGTERM, 0, 0, 0
        )
    except Exception:
        # Not Linux, no libc, or prctl not allowed. Without this hook
        # a SIGKILL'd CLI would orphan the agent process, but the
        # CLI's normal try/finally still covers clean exits.
        pass


def _ignore_sigint() -> None:
    """Make the agent process immune to Ctrl+C.

    Without this, the CLI and the agent share a process group, so
    Ctrl+C delivers SIGINT to both. The agent's main thread would
    raise KeyboardInterrupt at whatever it's executing — usually
    inside a tool call or LLM API request — printing its own
    traceback to the inherited stderr that the user sees.

    The CLI is the sole interpreter of human intent. Cancel arrives
    here over the pipe as a `cancel` event; final shutdown via
    `shutdown`. SIGINT ignored, SIGTERM still works for proc.terminate().
    """
    try:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
    except (ValueError, OSError):
        # ValueError on non-main thread; OSError on weird platforms.
        pass


def child_main(config: dict[str, Any], conn: Connection) -> None:
    """Subprocess entrypoint. Picklable so `multiprocessing.spawn` can
    target it.

    `config` keys:
      - cwd: absolute path to use as the child's working directory
      - model: provider string, optionally `provider/model-name`
      - session_id: existing session id (already created by upstream)
      - soul_path / tools_path / primer_path: resolved persona paths
        (inherited unchanged by subagents — they use the same SOUL,
        TOOLS, and PRIMER as the root)
      - approved_paths: list[str] replayed via `pre_approve` so the
        user isn't re-prompted for paths already accepted upstream
      - is_subagent: bool. When true, this is a subagent:
        - session lives at `session_root`
        - depth and parent_session_id are recorded on disk
        - role_body (optional) and task_body are layered onto the
          universal SOUL/TOOLS/PRIMER base by SystemPromptBuilder
    """
    _set_parent_death_signal()
    _ignore_sigint()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    state = _ChildState(conn=conn)
    try:
        agent, session, loaded_plugins = _bootstrap(config, state)
    except Exception as e:
        logger.exception("child bootstrap failed")
        state.send(
            "agent_error",
            kind=type(e).__name__,
            message=str(e),
            fatal=True,
        )
        try:
            conn.close()
        except Exception:
            pass
        return

    state.send("ready")

    io_thread = threading.Thread(
        target=state.io_loop, name="agent-io", daemon=True
    )
    io_thread.start()

    # Fire on_session_start AFTER ready + io_thread so cancel events
    # can route while plugins are initializing. The work_queue is not
    # dequeued until this returns — slow plugin startup hangs the
    # agent (intentional; same blast radius as a hung tool).
    #
    # If the user pressed Esc during plugin startup, the IO thread
    # set state.cancel_event. We honor it by short-circuiting the
    # remaining hooks and shutting down — otherwise _run_turn would
    # unconditionally clear the cancel on the first turn and mask
    # the user's intent.
    loaded_plugins.call_on_session_start(
        session, cancel_check=state.cancel_event.is_set
    )
    if state.cancel_event.is_set():
        state.send(
            "info",
            level="info",
            message="cancelled during plugin startup; shutting down",
        )
        state.shutdown_event.set()
        state.cancel_event.clear()

    while not state.shutdown_event.is_set():
        try:
            event = state.work_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if event.get("type") == "shutdown":
            break
        if event.get("type") != "user_prompt":
            logger.warning("main loop: unexpected event %r", event.get("type"))
            continue
        # Issue #68: turn_active gates the IO thread's user_note
        # handling — set while a turn is running so notes go onto
        # the mid-turn inbox, cleared when idle so notes that
        # arrive between turns get promoted to fresh prompts.
        state.turn_active.set()
        try:
            _run_turn(
                state,
                agent,
                session,
                prompt=event["prompt"],
                persist=event.get("persist", True),
            )
        finally:
            state.turn_active.clear()

    # Tear down background shell processes started by run_background
    # so a clean exit doesn't leave the user's dev server / watcher
    # lingering. SIGTERM with a 2s grace, then SIGKILL.
    try:
        signalled = agent_tools.shutdown_background(grace_s=2.0)
        if signalled:
            logger.info(
                "shutdown: signalled %d background process(es)", signalled
            )
    except Exception:
        logger.exception("shutdown: error tearing down background procs")

    # Tear down subagents first, then fire on_session_end. End-hooks
    # might write to disk or call APIs; they shouldn't race with
    # subagents that are still alive forwarding events upstream.
    _terminate_subagents(state, agent)
    try:
        loaded_plugins.call_on_session_end(session)
    except Exception:
        logger.exception("on_session_end teardown raised")

    try:
        conn.close()
    except Exception:
        pass
