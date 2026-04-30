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
            self.permission_replies.put(event)
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
        self.send("permission_request", target=str(target))
        try:
            reply = self.permission_replies.get(timeout=300)
        except queue.Empty:
            logger.warning("permission prompt timed out for %s", target)
            return False
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
    _add("execute", agent_tools.execute)
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
    # steal raw keystrokes from the CLI's CancelWatcher (which is the
    # process holding the controlling tty in cbreak mode).
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

    # Plugin loading. is_subagent is True for spawned subagents — the
    # plugins module honors `[load] in_subagents = false` to skip
    # plugins that aren't parallel-safe.
    loaded_plugins = plugins_mod.load(is_subagent=is_subagent)

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
            on_tool_call=lambda n, a: state.send(
                "tool_call_started", name=n, args=a
            ),
            on_tool_result=lambda n, c: state.send(
                "tool_result", name=n, content=c
            ),
            on_usage=lambda u: state.send(
                "usage",
                input=int(u.get("input", 0) or 0),
                output=int(u.get("output", 0) or 0),
                cache_creation=int(u.get("cache_creation", 0) or 0),
                cache_read=int(u.get("cache_read", 0) or 0),
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
        _run_turn(
            state,
            agent,
            session,
            prompt=event["prompt"],
            persist=event.get("persist", True),
        )

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
