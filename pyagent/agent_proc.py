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

from pyagent import config as config_mod
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
    _subagent_conns: dict[str, Connection] = field(default_factory=dict)
    _subagent_reply_queues: dict[str, queue.Queue] = field(default_factory=dict)
    _subagent_lock: threading.Lock = field(default_factory=threading.Lock)
    _descendants: dict[str, str] = field(default_factory=dict)
    agent: Any = None
    self_agent_id: str | None = None
    _pending_ask_replies: dict[str, queue.Queue] = field(default_factory=dict)
    _inbound_ask_sid: dict[str, str] = field(default_factory=dict)
    _ask_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_perm_replies: dict[str, queue.Queue] = field(default_factory=dict)
    _perm_lock: threading.Lock = field(default_factory=threading.Lock)
    turn_active: threading.Event = field(default_factory=threading.Event)
    _context_warn_tier: int = -1

    def send(self, event_type: str, **payload: Any) -> None:
        """Send a typed event upstream (CLI for root, parent agent for sub)."""
        with self.send_lock:
            try:
                protocol.send(self.conn, event_type, **payload)
            except (BrokenPipeError, OSError):
                self.shutdown_event.set()

    def _send_dict(self, event: dict) -> None:
        """Send a pre-built event dict."""
        with self.send_lock:
            try:
                self.conn.send(event)
            except (BrokenPipeError, OSError):
                self.shutdown_event.set()

    def register_subagent_pipe(self, sid: str, conn: Connection) -> queue.Queue:
        """Hook a new subagent's pipe into the IO loop's multiplex.

        Returns the per-subagent reply queue.
        """
        rq: queue.Queue = queue.Queue()
        with self._subagent_lock:
            self._subagent_conns[sid] = conn
            self._subagent_reply_queues[sid] = rq
        return rq

    def unregister_subagent_pipe(self, sid: str) -> None:
        """Remove a subagent from the multiplex set.

        Sweeps the descendants table too — every descendant whose
        path went through `sid` is now unreachable.
        """
        with self._subagent_lock:
            self._subagent_conns.pop(sid, None)
            self._subagent_reply_queues.pop(sid, None)
            stale = [d for d, via in self._descendants.items() if via == sid]
            for d in stale:
                self._descendants.pop(d, None)

    def _snapshot_conns(self) -> tuple[list[Connection], dict[int, str]]:
        """Snapshot current set of (parent + subagent) conns for wait()."""
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
                continue
            for c in ready:
                if c is self.conn:
                    self._handle_parent_event()
                else:
                    sid = fileno_to_sid.get(c.fileno())
                    if sid is None:
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
        target_sid = event.get("agent_id")
        if target_sid:
            with self._subagent_lock:
                direct_conn = self._subagent_conns.get(target_sid)
                via_child = (
                    None
                    if direct_conn is not None
                    else self._descendants.get(target_sid)
                )
                via_conn = (
                    self._subagent_conns.get(via_child)
                    if via_child is not None
                    else None
                )
            if direct_conn is not None:
                # strip agent_id so the direct child sees a normal "for me" event
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
                try:
                    via_conn.send(event)
                except (BrokenPipeError, OSError):
                    logger.warning(
                        "forward toward descendant %r via %r failed",
                        target_sid,
                        via_child,
                    )
                return
            logger.warning("drop event for unknown subagent %r: %r", target_sid, kind)
            return

        if kind == "user_prompt":
            self.work_queue.put(event)
        elif kind == "user_note":
            text = (event.get("text", "") or "").strip()
            if not text:
                logger.debug("user_note with empty text; dropping")
                return
            if self.turn_active.is_set():
                if self.agent is not None:
                    self.agent.pending_async_replies.put(f"[user adds]: {text}")
            else:
                self.work_queue.put({"type": "user_prompt", "prompt": text})
        elif kind == "cancel":
            self.cancel_event.set()
            try:
                killed = agent_tools.kill_active()
                if killed:
                    logger.info("cancel: killed %d active shell process(es)", killed)
            except Exception:
                logger.exception("cancel: error killing active shell")
            with self._subagent_lock:
                conns = list(self._subagent_conns.values())
            for c in conns:
                try:
                    c.send({"type": "cancel"})
                except (BrokenPipeError, OSError):
                    pass
        elif kind == "permission_response":
            req_id = event.get("request_id", "")
            if req_id:
                with self._perm_lock:
                    rq = self._pending_perm_replies.pop(req_id, None)
                if rq is not None:
                    rq.put(event)
                else:
                    logger.warning(
                        "permission_response for unknown request_id %r; " "dropping",
                        req_id,
                    )
            else:
                self.permission_replies.put(event)
        elif kind == "parent_answer":
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
            text = event.get("text", "") or ""
            if self.agent is not None:
                self.agent.pending_async_replies.put(f"[parent says]: {text}")
        elif kind == "set_model":
            self._handle_set_model(event.get("model", ""))
        elif kind == "shutdown":
            self.shutdown_event.set()
            self.work_queue.put({"type": "shutdown"})
        else:
            logger.warning("child: unknown event type %r", kind)

    def _handle_set_model(self, model: str) -> None:
        """Swap the agent's LLM client to a new model."""
        if self.agent is None or not model:
            return
        try:
            new_client = get_client(model)
        except Exception as e:
            self.send(
                "info",
                level="warn",
                message=(f"set_model {model!r} failed: " f"{type(e).__name__}: {e}"),
            )
            return
        self.agent.client = new_client
        self.send("info", level="info", message=f"model swapped to {model}")

    def _handle_subagent_event(self, sid: str, conn: Connection) -> None:
        try:
            event = conn.recv()
        except (EOFError, OSError):
            with self._subagent_lock:
                unexpected = sid in self._subagent_conns
            self.unregister_subagent_pipe(sid)
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
        if kind in ("ready", "turn_complete", "agent_error"):
            if inner_id is None:
                routed_async = False
                if kind == "turn_complete" and self.agent is not None:
                    entry = self.agent._subagents.get(sid)
                    if entry is not None and getattr(entry, "mode", None) == "async":
                        text = event.get("final_text", "") or ""
                        formatted = f"[subagent {entry.name} ({sid}) reports]: {text}"
                        self.agent.pending_async_replies.put(formatted)
                        entry.mode = None
                        routed_async = True
                if not routed_async:
                    with self._subagent_lock:
                        rq = self._subagent_reply_queues.get(sid)
                    if rq is not None:
                        rq.put(event)
            else:
                with self._subagent_lock:
                    self._descendants[inner_id] = sid
            if kind != "turn_complete":
                self._forward_upstream(event, sid)
            return
        if kind == "subagent_ask":
            # ask_parent always targets the IMMEDIATE parent; bubbled variants are a bug
            if inner_id is not None:
                logger.warning(
                    "subagent_ask bubbled past its direct parent "
                    "(inner_id=%r via sid=%r); dropping",
                    inner_id,
                    sid,
                )
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
                    f"[subagent {name} ({sid}) asks (req={req_id})]: " f"{question}"
                )
                self.agent.pending_async_replies.put(formatted)
            self._forward_upstream(event, sid)
            return
        if kind == "subagent_note":
            # notes always target the IMMEDIATE parent; drop bubbled variants
            if inner_id is not None:
                logger.warning(
                    "subagent_note bubbled past its direct parent "
                    "(inner_id=%r via sid=%r); dropping",
                    inner_id,
                    sid,
                )
                self._forward_upstream(event, sid)
                return
            severity = event.get("severity", "info") or "info"
            text = event.get("text", "") or ""
            if self.agent is not None:
                self.agent._append_subagent_note(sid, severity, text)
                entry = self.agent._subagents.get(sid)
                name = entry.name if entry else sid
                formatted = f"[subagent {name} ({sid}) notes ({severity})]: " f"{text}"
                self.agent.pending_async_replies.put(formatted)
            self._forward_upstream(event, sid)
            return
        if inner_id is not None and inner_id != sid:
            with self._subagent_lock:
                self._descendants[inner_id] = sid
        self._forward_upstream(event, sid)

    def _forward_upstream(self, event: dict, sid: str) -> None:
        out = dict(event)
        out.setdefault("agent_id", sid)
        self._send_dict(out)

    def permission_handler(self, target: Path) -> bool:
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
                    target,
                    req_id,
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
    state: _ChildState | None = None,
    parent_session: Session | None = None,
    base_config: dict[str, Any] | None = None,
    allow_meta: bool = False,
    allowlist: list[str] | None = None,
    checklist: Checklist | None = None,
) -> None:
    """Register the default tool set on `agent`.

    `allow_meta=True` registers spawn_subagent / call_subagent /
    terminate_subagent on top of the default set.

    `allowlist` (when non-None) restricts registration to only the
    named tools; names not in the default set are silently ignored.
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
    _add("run_background", agent_tools.run_background, auto_offload=False)
    _add("read_output", agent_tools.read_output)
    _add("wait_for", agent_tools.wait_for, auto_offload=False)
    _add("kill_process", agent_tools.kill_process, auto_offload=False)
    _add("fetch_url", agent_tools.fetch_url)
    _add(
        "read_skill",
        skills_mod.read_skill,
        auto_offload=False,
        evict_after_use=True,
    )
    if checklist is not None:
        _add("add_task", make_add_task(checklist), auto_offload=False)
        _add("update_task", make_update_task(checklist), auto_offload=False)
        _add("list_tasks", make_list_tasks(checklist), auto_offload=False)
    if state is not None and state.self_agent_id is not None:
        _add(
            "ask_parent",
            subagent_mod.make_ask_parent(state, agent),
            auto_offload=False,
        )
        _add(
            "notify_parent",
            subagent_mod.make_notify_parent(state, agent),
            auto_offload=False,
        )
    if allow_meta:
        assert (
            state is not None and parent_session is not None and base_config is not None
        )
        _add(
            "spawn_subagent",
            subagent_mod.make_spawn_subagent(state, agent, parent_session, base_config),
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
        _add(
            "reply_to_subagent",
            subagent_mod.make_reply_to_subagent(state, agent),
            auto_offload=False,
        )
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
    """Replicate the CLI's startup setup inside the child process."""
    # close stdin so a buggy input() call can't steal raw keystrokes from prompt_toolkit
    try:
        sys.stdin.close()
    except OSError:
        pass
    sys.stdin = open(os.devnull)  # noqa: SIM115

    os.chdir(config["cwd"])
    permissions.set_workspace(config["cwd"])
    permissions.pre_approve(paths.config_dir())
    for p in config.get("approved_paths", []):
        permissions.pre_approve(p)
    permissions.set_prompt_handler(state.permission_handler)

    is_subagent = bool(config.get("is_subagent"))
    state.self_agent_id = config["session_id"] if is_subagent else None

    # must run before get_client: plugins can register LLM providers
    loaded_plugins = plugins_mod.load(is_subagent=is_subagent)

    client = get_client(config["model"])

    allow_meta = bool(config.get("role_meta_tools", True))
    allowlist = config.get("role_tools")
    catalog_for_roles = roles_mod.catalog if allow_meta else ""

    cap_mb = config_mod.resolve_attachment_dir_cap_mb(
        config.get("attachment_dir_cap_mb")
    )

    if is_subagent:
        session = Session(
            session_id=config["session_id"],
            root=Path(config["session_root"]),
            attachment_dir_cap_mb=cap_mb,
        )
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
            include_soul=False,
        )
    else:
        session = Session(
            session_id=config["session_id"],
            attachment_dir_cap_mb=cap_mb,
        )
        role_body = config.get("role_body", "")
        system = SystemPromptBuilder(
            soul=Path(config["soul_path"]),
            tools=Path(config["tools_path"]),
            primer=Path(config["primer_path"]),
            skills_catalog=skills_mod.live_catalog,
            roles_catalog=catalog_for_roles,
            role_body=role_body,
            plugin_loader=loaded_plugins,
            include_soul=not role_body,
        )

    loaded_plugins.bind_session(session)

    agent = Agent(
        client=client,
        system=system,
        session=session,
        depth=int(config.get("depth", 0)),
        plugins=loaded_plugins,
    )
    state.agent = agent

    if not is_subagent:

        def _emit_notes_unread(count: int, by_severity: dict[str, int]) -> None:
            state.send("notes_unread", count=count, by_severity=by_severity)

        agent._notes_unread_emitter = _emit_notes_unread

    if not is_subagent:
        checklist = Checklist(
            session.dir / "checklist.json",
            on_change=lambda tasks: state.send("checklist", tasks=tasks),
        )
        if checklist.tasks:
            state.send("checklist", tasks=checklist.list())
    else:
        checklist = None

    _register_tools(
        agent,
        state=state,
        parent_session=session,
        base_config=config,
        allow_meta=allow_meta,
        allowlist=allowlist,
        checklist=checklist,
    )

    builtin_names = set(agent.tools.keys())
    role_only = loaded_plugins.role_only_tool_names()
    for tool_name, (plugin_name, fn) in loaded_plugins.tools().items():
        if tool_name in builtin_names:
            logger.warning(
                "plugin %s: tool %r conflicts with a built-in; skipping",
                plugin_name,
                tool_name,
            )
            continue
        if tool_name in role_only and (allowlist is None or tool_name not in allowlist):
            continue
        agent.add_tool(tool_name, fn)

    loaded_plugins.bind_agent(agent)

    agent.conversation = session.load_history()
    if agent.conversation:
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


def _emit_context_status(state: _ChildState, agent: Agent) -> None:
    """Emit one `context_status` event for the footer plus, on a
    tier crossing, one `info` event for the chat.
    """
    client = getattr(agent, "client", None)
    if client is None:
        return
    # prefer effective_context_window: Ollama caps num_ctx below the architectural max
    window = int(
        getattr(
            client,
            "effective_context_window",
            getattr(client, "context_window", 0),
        )
        or 0
    )
    if window <= 0:
        return
    last_usage: dict = {}
    for turn in reversed(agent.conversation):
        if isinstance(turn, dict) and isinstance(turn.get("usage"), dict):
            last_usage = turn["usage"]
            break
    used = int(last_usage.get("input", 0) or 0)
    if used <= 0:
        return
    pct = max(0, min(100, int(used * 100 / window)))
    state.send("context_status", pct=pct, used=used, window=window)

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
            on_text_delta=lambda t: state.send("assistant_text_delta", text=t),
            on_tool_call=lambda n, a: state.send("tool_call_started", name=n, args=a),
            on_tool_result=lambda n, c: state.send("tool_result", name=n, content=c),
            on_usage=lambda u: (
                state.send(
                    "usage",
                    input=int(u.get("input", 0) or 0),
                    output=int(u.get("output", 0) or 0),
                    cache_creation=int(u.get("cache_creation", 0) or 0),
                    cache_read=int(u.get("cache_read", 0) or 0),
                ),
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
        del agent.conversation[saved:]
    state.send("turn_complete", final_text=final_text)


def _terminate_subagents(state: _ChildState, agent: Agent) -> None:
    """Best-effort shutdown of all live subagents on this agent's exit."""
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
    """Best-effort: ask the kernel to SIGTERM us if the parent dies (Linux-only)."""
    try:
        import ctypes
        import signal as _signal

        # PR_SET_PDEATHSIG = 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _signal.SIGTERM, 0, 0, 0)
    except Exception:
        pass


def _ignore_sigint() -> None:
    """Make the agent process immune to Ctrl+C; cancel arrives via pipe."""
    try:
        import signal as _signal

        _signal.signal(_signal.SIGINT, _signal.SIG_IGN)
    except (ValueError, OSError):
        pass


def child_main(config: dict[str, Any], conn: Connection) -> None:
    """Subprocess entrypoint. Picklable so `multiprocessing.spawn` can
    target it.
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

    io_thread = threading.Thread(target=state.io_loop, name="agent-io", daemon=True)
    io_thread.start()

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

    try:
        signalled = agent_tools.shutdown_background(grace_s=2.0)
        if signalled:
            logger.info("shutdown: signalled %d background process(es)", signalled)
    except Exception:
        logger.exception("shutdown: error tearing down background procs")

    # terminate subagents before on_session_end so end-hooks don't race with forwarding
    _terminate_subagents(state, agent)
    try:
        loaded_plugins.call_on_session_end(session)
    except Exception:
        logger.exception("on_session_end teardown raised")

    try:
        conn.close()
    except Exception:
        pass
