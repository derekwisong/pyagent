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

This first cut is **single-level** — subagents do not themselves get
the meta-tools registered, so the depth cap in practice acts as 1 even
when the config allows more. Recursion will lift that restriction in
a follow-up without changing the cap semantics.
"""

from __future__ import annotations

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
) -> tuple[str, dict[str, Any]]:
    """Build the subagent's spawn config. Returns (subagent_id, config)."""
    sid = f"{name}-{uuid.uuid4().hex[:8]}"
    session_root = parent_session.dir / "subagents"
    cfg = dict(base_config)
    cfg["session_id"] = sid
    cfg["session_root"] = str(session_root)
    cfg["depth"] = parent_depth + 1
    cfg["parent_session_id"] = parent_session.id
    cfg["system_prompt_override"] = system_prompt
    cfg["is_subagent"] = True
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

    def spawn_subagent(name: str, system_prompt: str) -> str:
        """Spawn a subagent in its own subprocess with a custom system prompt.

        The subagent has the same default tool set as the parent (read_file,
        write_file, list_directory, grep, execute, fetch_url, read_ledger,
        write_ledger, read_skill). It cannot itself spawn further subagents
        in this version. Use `call_subagent(<id>, message)` to send it work,
        and `terminate_subagent(<id>)` when you're done with it.

        Args:
            name: Short label for this subagent (e.g. "researcher",
                "fact-checker"). Becomes part of the subagent id and the
                CLI's display prefix for events from this child.
            system_prompt: Full system prompt for the subagent. Replaces
                the parent's persona for this child — write whatever
                instructions, role, or constraints you want it to follow.

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

        sid, sub_config = _build_subagent_config(
            name=name,
            system_prompt=system_prompt,
            base_config=base_config,
            parent_session=parent_session,
            parent_depth=agent.depth,
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
