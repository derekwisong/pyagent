import collections
import logging
import queue
import threading
import time
from typing import Any, Callable

from pyagent.llms import LLMClient
from pyagent.plugins import (
    LoadedPlugins,
    format_missing_tool_error,
    make_prompt_context,
)
from pyagent.prompts import SystemPromptBuilder
from pyagent.session import Attachment, Session
from pyagent.tool_schema import schema

logger = logging.getLogger(__name__)


class Agent:
    """A simple tool-using LLM agent.

    Tools are plain Python functions; their type hints and docstrings are
    used to generate the schema sent to the model.

    Attributes:
        system: Optional system prompt.
        session: Optional session for persistent history and attachment offload.
        tools: Mapping of tool name to the underlying Python function.
        conversation: Append-only list of turns exchanged with the model.

    Example:
        >>> from pyagent.llms.anthropic import AnthropicClient
        >>> def add(a: int, b: int) -> int:
        ...     '''Add two integers.'''
        ...     return a + b
        >>> agent = Agent(
        ...     client=AnthropicClient(),
        ...     system="You are a helpful assistant.",
        ... )
        >>> agent.add_tool("add", add)
        >>> agent.run("What is 2 + 3?")
    """

    def __init__(
        self,
        client: LLMClient,
        system: str | SystemPromptBuilder | None = None,
        session: Session | None = None,
        depth: int = 0,
        plugins: LoadedPlugins | None = None,
    ) -> None:
        self.client = client
        self.system = system
        self.session = session
        self.tools: dict[str, Callable[..., Any]] = {}
        self._auto_offload: dict[str, bool] = {}
        # Tools opted into post-consumption eviction. After an
        # assistant turn produces output, any earlier tool_result for
        # one of these tools is replaced in-memory by a one-line stub
        # — the data was single-shot reference content, the model
        # already extracted what it needed from it, and recovery is
        # one tool call away. JSONL on disk keeps the full content
        # (round-trip invariant); eviction is in-memory only.
        # See issue #10.
        self._evict_after_use: dict[str, bool] = {}
        # Side channel set by `_render_tool_result` whenever a tool
        # result writes an attachment to disk. The agent loop reads
        # this immediately after `_route_tool` returns and copies
        # the metadata onto the tool_result entry as a structured
        # `attachment` field, so audit / replay tools don't have to
        # regex the stub out of the tool_result `content` prose.
        # Reset to None on every render call.
        self._last_tool_attachment: dict[str, Any] | None = None
        self.conversation: list[Any] = []
        self.plugins = plugins
        # Subagent registry: id -> opaque entry (shape owned by the
        # subagent module). Exposed on Agent so meta-tools registered
        # via add_tool can mutate it from inside _route_tool.
        self.depth: int = depth
        self._subagents: dict[str, Any] = {}
        # Cumulative token usage across every LLM call this agent has
        # made in its lifetime. Updated after each `_call_llm` from
        # the `usage` block returned by the LLM client. The
        # `on_usage` callback fired in `run` lets agent_proc forward
        # per-call deltas upstream so the CLI can render a running
        # cost meter.
        self.token_usage: dict[str, int] = {
            "input": 0,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
        }
        # Async subagent inbox. The IO thread (in agent_proc) puts
        # formatted reply strings here when an async-fired subagent
        # finishes its turn. `_drain_pending_async`, called at the
        # top of each `run` loop iteration, appends them to
        # `conversation` as user-role messages so the LLM sees
        # them on its very next API call. Thread-safe queue —
        # the IO thread is what produces, the main thread (where
        # run() executes) is what consumes.
        self.pending_async_replies: queue.Queue = queue.Queue()
        # Per-sid notification ring (issue #64). The IO thread
        # appends a `(seq, ts, severity, text)` tuple per inbound
        # `subagent_note`. `deque(maxlen=N)` drops the oldest on
        # overflow; the dropped count is tracked separately on
        # `_subagent_note_drops` so peek (issue #65) can surface a
        # synthetic `... (N notes dropped) ...` line covering the
        # gap between cursor and the smallest seq still in the
        # ring. Inbox delivery does NOT consume — peek reads
        # history from this ring, while the inbox surfaces notes
        # at turn boundaries via `pending_async_replies`.
        self._subagent_notes: dict[str, "collections.deque"] = {}
        self._subagent_note_seq: dict[str, int] = {}
        self._subagent_note_drops: dict[str, int] = {}
        self._notes_lock = threading.Lock()
        # Wallclock anchor per subagent so peek output can render
        # `t+12s` relative to spawn rather than an absolute time.
        # The IO thread sets this on first note; cleared when the
        # subagent terminates (issue #65).
        self._subagent_note_t0: dict[str, float] = {}
        # Unread-notes counters surfaced to the CLI footer (issue #67
        # depends on this). Increment on append, reset on drain;
        # `_notes_unread_emitter` (wired by agent_proc bootstrap on
        # the root agent only) ships the deltas as `notes_unread`
        # events so the CLI can render `msgs:N` without polling.
        self._unread_notes_total: int = 0
        self._unread_notes_by_severity: dict[str, int] = {
            "info": 0, "warn": 0, "alert": 0,
        }
        self._notes_unread_emitter: Callable[
            [int, dict[str, int]], None
        ] | None = None

    def add_tool(
        self,
        name: str,
        fn: Callable[..., Any],
        auto_offload: bool = True,
        *,
        evict_after_use: bool = False,
    ) -> None:
        """Register a tool.

        `evict_after_use=True` opts the tool into post-consumption
        eviction: once an assistant turn has produced output that
        followed the tool_result, the result's `content` field is
        replaced in-memory by a short stub (see `_apply_eviction`).
        Use for single-shot reference content (skill bodies, etc.) —
        not for tools whose results the model may want to revisit.
        """
        self.tools[name] = fn
        self._auto_offload[name] = auto_offload
        self._evict_after_use[name] = evict_after_use

    def _tool_schemas(self) -> list[dict[str, Any]]:
        """Build JSON-schema tool definitions from each tool's type hints + docstring."""
        return [schema(name, fn) for name, fn in self.tools.items()]

    def _system_prompt_segments(self) -> tuple[str | None, str | None]:
        """Return (stable, volatile) for the current turn.

        Plugins contributing volatile prompt sections place their
        rendered output in the volatile segment; LLM clients position
        it after the cache_control breakpoint so dynamic content
        doesn't invalidate the cached system prefix.
        """
        if isinstance(self.system, SystemPromptBuilder):
            ctx = make_prompt_context(self.conversation)
            stable, volatile = self.system.build_segments(ctx)
            return (stable or None, volatile or None)
        if self.system is None:
            return (None, None)
        return (self.system, None)

    def _call_llm(
        self,
        messages: list[Any],
        system: str | None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> Any:
        """One model turn. Sends conversation + tool schemas, returns the assistant turn.

        `on_text_delta`, when set, receives incremental text chunks as
        the provider streams them. The returned dict still carries the
        fully-accumulated `text` field — streaming is a UX channel,
        not a flow-control change. Providers that haven't implemented
        streaming silently ignore the callback and return the same
        dict at end-of-call.
        """
        return self.client.respond(
            conversation=messages,
            system=system,
            tools=self._tool_schemas(),
            system_volatile=system_volatile,
            on_text_delta=on_text_delta,
        )

    def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        if name not in self.tools:
            provenance = (
                self.plugins.declared_tool_provenance
                if self.plugins is not None
                else {}
            )
            return format_missing_tool_error(
                name=name,
                available=list(self.tools.keys()),
                declared_tool_provenance=provenance,
            )
        result = self.tools[name](**args)
        return self._render_tool_result(name, result, args=args)

    # Hard ceiling: any tool result above this size is offloaded
    # regardless of the tool's `auto_offload` setting. Prevents a
    # runaway read_file (auto_offload=False but actual file is huge)
    # from blowing the request size on the next turn.
    HARD_OFFLOAD_CEILING = 64_000

    # Tool-call args that exceed this size get scrubbed from the
    # conversation after the tool runs. Prevents a write_file with
    # 50KB of content from re-sending those bytes on every subsequent
    # turn — the tool already executed, the result describes the
    # outcome, and the bytes live at the file path on disk if anyone
    # needs to recover them.
    TOOL_ARG_ELIDE_THRESHOLD = 4_000

    # Tools that bypass auto_offload but still get the soft threshold
    # applied — `read_file` is registered with auto_offload=False so a
    # small explicit read returns inline, but the soft threshold
    # (8000 chars by default) still needs to fire on a runaway ranged
    # read of HTML / a wide log line / a binary mistakenly read as text.
    SOFT_THRESHOLD_FORCED_TOOLS: frozenset[str] = frozenset({"read_file"})

    def _render_tool_result(
        self,
        name: str,
        result: Any,
        args: dict[str, Any] | None = None,
    ) -> str:
        # Reset every call so the caller in `agent.run` reads either
        # the metadata for *this* tool's attachment or a clean None
        # when the tool produced inline-only output.
        self._last_tool_attachment = None
        if isinstance(result, Attachment):
            if not self.session:
                # No session → nothing to save. Prefer inline_text if
                # the tool supplied one (it's the human answer); else
                # fall back to today's preview-or-content behavior.
                if result.inline_text is not None:
                    return result.inline_text
                if result.preview:
                    return result.preview
                return result.content if isinstance(result.content, str) else ""
            path = self.session.write_attachment(
                name, result.content, result.suffix
            )
            self._last_tool_attachment = {
                "path": str(path),
                "size_bytes": (
                    len(result.content)
                    if isinstance(result.content, (str, bytes))
                    else 0
                ),
            }
            if result.inline_text is not None:
                # inline_text path: the saved file is *side data*
                # (structured blob the agent might legitimately re-read
                # via extract_doc / read_file), not an offloaded big
                # result. Skip the offload header / "do not read" warn;
                # use a footer that's explicit about both halves —
                # "inline above is complete" so the agent doesn't
                # reflexively re-read for missing content, and
                # "for downstream tools" so the LLM knows when reading
                # IS appropriate (chaining, structured-input consumers).
                return (
                    f"{result.inline_text}\n\n[also saved: {path} — "
                    f"inline answer above is complete; attachment is "
                    f"for downstream tools]"
                )
            cap = self.session.attachment_threshold
            return self._format_offload_ref(
                path,
                len(result.content),
                result.preview,
                cap_chars=cap,
                tool_name=name,
            )

        if isinstance(result, str):
            text = result
        elif isinstance(result, list):
            # Join with newlines so range-based reads on the offloaded
            # attachment (read_file start/end, head -N) actually slice.
            # str(list) would put everything on a single repr line.
            text = "\n".join(str(item) for item in result)
        else:
            text = str(result)
        if self.session:
            auto = self._auto_offload.get(name, True)
            over_threshold = len(text) > self.session.attachment_threshold
            over_ceiling = len(text) > self.HARD_OFFLOAD_CEILING
            forced_soft = (
                not auto
                and name in self.SOFT_THRESHOLD_FORCED_TOOLS
                and over_threshold
            )
            if (auto and over_threshold) or over_ceiling or forced_soft:
                path = self.session.write_attachment(name, text)
                self._last_tool_attachment = {
                    "path": str(path),
                    "size_bytes": len(text),
                }
                preview = text[: self.session.preview_chars]
                # File-shape and read_file range hints are computed
                # from the rendered text + the original tool args so
                # the agent can size its next slice without having to
                # bisect by feel (issue #82).
                file_lines = text.count("\n") + 1 if text else 0
                range_consumed: tuple[int, int] | None = None
                next_call_hint: str | None = None
                if name == "read_file" and isinstance(args, dict):
                    rf_path = args.get("path")
                    rf_start_raw = args.get("start", 1)
                    rf_end_raw = args.get("end")
                    try:
                        rf_start = int(rf_start_raw)
                    except (TypeError, ValueError):
                        rf_start = 1
                    rf_consumed_end = rf_start + file_lines - 1
                    range_consumed = (rf_start, rf_consumed_end)
                    # Was this slice the tail of the file? If `end`
                    # is unset the agent asked for "to EOF" — but
                    # `read_file` itself caps at 2000 lines and
                    # appends a `... (truncated: ...)` marker when
                    # it had to truncate, so check for that instead
                    # of trusting the args. With explicit end, the
                    # next start is one past what we just returned.
                    end_is_eof = rf_end_raw is None
                    truncated_marker = "... (truncated: file has "
                    file_was_truncated = truncated_marker in text
                    if end_is_eof and not file_was_truncated:
                        next_call_hint = "[whole file consumed]"
                    elif rf_path is not None:
                        next_start = rf_consumed_end + 1
                        next_call_hint = (
                            f"[next: read_file({rf_path}, "
                            f"start={next_start}) — you've read lines "
                            f"{rf_start}-{rf_consumed_end}]"
                        )
                return self._format_offload_ref(
                    path,
                    len(text),
                    preview,
                    cap_chars=self.session.attachment_threshold,
                    file_lines=file_lines,
                    range_consumed=range_consumed,
                    next_call_hint=next_call_hint,
                    tool_name=name,
                )
        return text

    def _scrub_large_tool_args(self, call: dict[str, Any]) -> None:
        """Replace any oversized string arg with a short marker.

        Called after a tool has finished executing. The original args
        were what we sent into the tool; once the tool has run, those
        bytes don't need to ride along on every subsequent LLM call.
        Eliding them here means a write_file with 50KB of content
        costs 50KB on the turn it ran and a few hundred bytes
        thereafter, instead of 50KB forever.

        The tool result message describes what happened (path, byte
        count, success/error). If the agent later needs the actual
        content, it can read_file the path and the result will
        auto-offload via the attachment system. No information is
        lost, just bytes deduplicated against on-disk state.

        Mutates in place — `call["args"]` is a reference to the dict
        inside `self.conversation`, so the change persists into next
        turn's `_call_llm` payload and into the saved session.
        """
        args = call.get("args")
        if not isinstance(args, dict):
            return
        for key, val in list(args.items()):
            if (
                isinstance(val, str)
                and len(val) > self.TOOL_ARG_ELIDE_THRESHOLD
            ):
                args[key] = (
                    f"<{len(val)} chars elided after tool ran; "
                    f"see the tool result for the outcome>"
                )

    # Per-tool next-step hints rendered into the offload header when no
    # tool-specific guidance has already been computed (read_file gets
    # its own range-aware hint via `next_call_hint`; grep already
    # appends a "tighten the pattern" marker on truncation in
    # tools.py). See issue #82.
    _TOOL_HINTS: dict[str, str] = {
        "execute": (
            "stdout was huge — filter (`grep`/`head`) or redirect to a "
            "file"
        ),
        "fetch_url": (
            "`read_file` the saved path with a smaller range, or "
            "`html_select` (researcher role) for a CSS slice"
        ),
    }

    @staticmethod
    def _format_offload_ref(
        path: Any,
        size: int,
        preview: str,
        *,
        cap_chars: int | None = None,
        file_lines: int | None = None,
        range_consumed: tuple[int, int] | None = None,
        next_call_hint: str | None = None,
        tool_name: str | None = None,
    ) -> str:
        """Render the offload notice the agent sees in place of bytes.

        The first line is a structured header keyed `produced` /
        `cap` / `file` so the agent can size its next slice on the
        first try instead of bisecting (issue #82). Older inline
        prose has been dropped — for `read_file` the next-range
        hint replaces the generic warning, for other tools the hint
        comes from `_TOOL_HINTS` keyed off `tool_name`.

        Note: `range_consumed` is currently included on the next-step
        line for `read_file` rather than the header; kept as a
        parameter so future callers can surface it independently
        (e.g. tail/head-style readers) without refactoring.
        """
        # Header: structured tokens. Keep keys / order stable —
        # `pyagent.sessions_audit._OFFLOAD_RE` parses this prefix to
        # identify offloaded results vs inline ones.
        parts = [f"[offload {path}", f"produced {size}c"]
        if cap_chars is not None:
            parts.append(f"cap {cap_chars}c")
        if file_lines is not None and file_lines > 0:
            avg = max(1, size // file_lines) if file_lines else 0
            parts.append(f"file {file_lines} lines, ~{avg}c/line avg")
        header = " | ".join(parts) + "]"

        # Next-step line: read_file gets a range-aware hint computed
        # by the caller; other tools fall back to the static hint
        # table. Some tools (`grep`) emit their own truncation hint
        # inside the result body — they get nothing extra here.
        hint_line: str | None = None
        if next_call_hint:
            hint_line = next_call_hint
        elif tool_name and tool_name in Agent._TOOL_HINTS:
            hint_line = f"[hint: {Agent._TOOL_HINTS[tool_name]}]"
        # `range_consumed` is not separately rendered today (the
        # range info already lives in `next_call_hint`); reserved for
        # future tool-shapes. Reference it so static analysis doesn't
        # flag it.
        _ = range_consumed

        lines = [header]
        if hint_line:
            lines.append(hint_line)
        if preview:
            lines.append("--- preview ---")
            lines.append(preview)
        return "\n".join(lines)

    def _route_tool(
        self,
        call: dict[str, Any],
        on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
    ) -> str:
        """Dispatch a single tool call and return the rendered result string.

        The single seam for tool execution. Future meta-tools that mutate
        agent state (e.g. spawn_subagent registering a child in a registry)
        will dispatch from here without further surgery on `run`. Exceptions
        are caught and returned as strings so the caller never has to
        compose tool results around a half-broken batch.

        Plugin v2 controlling-hook semantics live here:

        - ``before_tool`` runs *before* ``_execute_tool`` (which is
          where permission checks fire, inside the wrapped tool body),
          so a ``decision="block"`` short-circuits before any
          permission prompt reaches the human. Preserve this ordering
          — ``smoke_controlling_hooks.test_block_short_circuits_before_permission``
          locks it in.
        - ``decision="mutate"`` swaps the args dict in place inside
          the conversation so subsequent turns see the args the tool
          was actually invoked with (and arg-scrubbing applies to the
          mutated bytes, not the originals).
        - ``after_tool`` ``replace_result`` rewrites the bytes that
          land in the tool_result.
        - ``extra_user_message`` from either hook is pushed onto
          ``pending_async_replies`` so the next assistant turn sees it
          as a user-role message tagged with the originating plugin.
        """
        name = call["name"]
        args = call["args"]
        if on_tool_call:
            on_tool_call(name, args)
        if self.plugins is not None:
            before = self.plugins.call_before_tool_call(name, args)
            for note in before.extra_user_messages:
                self.pending_async_replies.put(note)
            if before.mutated and before.args is not args:
                # Persist the mutated args back into the tool_call
                # dict in self.conversation so future turns and
                # session replay see the args the tool actually ran
                # with. The reference swap matters: arg-scrubbing
                # below operates on `call["args"]`.
                call["args"] = before.args
                args = before.args
            if before.blocked:
                content = (
                    f"<blocked by plugin {before.block_plugin}: "
                    f"{before.block_reason}>"
                )
                logger.info(
                    "plugin=%s tool=%s reason=%s",
                    before.block_plugin,
                    name,
                    before.block_reason,
                )
                if on_tool_result:
                    on_tool_result(name, content)
                # No tool ran, so nothing to scrub. Skip the after_tool
                # hooks — the contract is "after the tool runs"; no
                # tool ran.
                return content
        is_error = False
        try:
            content = self._execute_tool(name, args)
        except Exception as e:
            logger.exception("tool %s raised", name)
            content = f"Error: {type(e).__name__}: {e}"
            is_error = True
        else:
            # Errors-as-data convention: tools encode refusals /
            # failures as `<…>`-prefixed strings. See
            # `pyagent.tools.is_error_result` for the full contract.
            from pyagent.tools import is_error_result
            is_error = is_error_result(content)
        if self.plugins is not None:
            after = self.plugins.call_after_tool_call(
                name, args, content, is_error
            )
            for note in after.extra_user_messages:
                self.pending_async_replies.put(note)
            if after.replaced:
                # AfterToolHookResult.replace_result is typed as
                # `str | None`; the dispatch loop already drops
                # non-strings with a warning, so this is safe.
                content = after.result
        if on_tool_result:
            on_tool_result(name, content)
        # Scrub bulky string args from the conversation so they don't
        # re-cost on every subsequent turn. Mutates in place — `args`
        # is a reference to the dict inside `self.conversation`.
        self._scrub_large_tool_args(call)
        return content

    @staticmethod
    def _eviction_stub(tool_name: str) -> str:
        return (
            f"[skill {tool_name!r} loaded earlier; content evicted "
            f"to save context. Call read_skill({tool_name!r}) again "
            f"to reload.]"
        )

    @staticmethod
    def _assistant_turn_has_output(msg: Any) -> bool:
        if not isinstance(msg, dict):
            return False
        if msg.get("role") != "assistant":
            return False
        if msg.get("content"):
            return True
        if msg.get("tool_calls"):
            return True
        return False

    def _apply_eviction(self) -> int:
        """Replace consumed eviction-flagged tool_result content with stubs.

        Provable-staleness rule: a tool_result entry for a tool
        registered with `evict_after_use=True` is stale once at least
        one later assistant turn in the conversation has produced
        output (text and/or tool_calls). The MOST RECENT such result
        (no following assistant turn yet) is still load-bearing — the
        agent is mid-consumption — so it is preserved.

        Idempotent: a result whose content is already the stub is a
        no-op on subsequent walks. JSONL on disk is not touched (see
        issue #10 design notes; smoke_session_replay locks the
        round-trip invariant).

        Returns the number of result entries newly stubbed.
        """
        if not any(self._evict_after_use.values()):
            return 0

        # Build a forward-marching set of indices into self.conversation
        # where an assistant turn with output appears. A tool_result at
        # index i is stale iff there's at least one such assistant
        # index > i.
        assistant_with_output: list[int] = [
            i for i, msg in enumerate(self.conversation)
            if self._assistant_turn_has_output(msg)
        ]
        if not assistant_with_output:
            return 0
        last_with_output = assistant_with_output[-1]

        stubbed = 0
        for i, msg in enumerate(self.conversation):
            if not isinstance(msg, dict):
                continue
            results = msg.get("tool_results")
            if not results:
                continue
            # Stale only if SOME assistant turn with output appears
            # later in the log. If the only assistant-with-output
            # indices are all <= i, this batch is the most recent —
            # leave it alone.
            if i >= last_with_output:
                continue
            for r in results:
                name = r.get("name")
                if not name or not self._evict_after_use.get(name, False):
                    continue
                stub = self._eviction_stub(name)
                if r.get("content") == stub:
                    continue  # already evicted; idempotent
                r["content"] = stub
                stubbed += 1
        return stubbed

    _NOTES_RING_MAXLEN = 64

    def _append_subagent_note(
        self, sid: str, severity: str, text: str
    ) -> tuple[int, float]:
        """Append a note to a subagent's per-sid ring (issue #64).

        Allocates the ring on first use. Increments the per-sid
        seq counter — monotonic, never reused even when overflow
        evicts the entry the seq was paired with. On overflow the
        deque drops the leftmost entry and `_subagent_note_drops`
        increments; #65's peek surfaces the gap as a synthetic
        `... (N notes dropped) ...` line at read time, which keeps
        the cursor honest without trying to maintain a marker
        entry inside a self-evicting ring.

        Returns the (seq, ts) of the appended note. ts is
        monotonic seconds since the first note for this sid so
        peek output can render `t+Ns` relative to that anchor.
        """
        with self._notes_lock:
            ring = self._subagent_notes.get(sid)
            if ring is None:
                ring = collections.deque(maxlen=self._NOTES_RING_MAXLEN)
                self._subagent_notes[sid] = ring
                self._subagent_note_seq[sid] = 0
                self._subagent_note_drops[sid] = 0
                self._subagent_note_t0[sid] = time.monotonic()
            seq = self._subagent_note_seq[sid]
            self._subagent_note_seq[sid] = seq + 1
            ts = time.monotonic() - self._subagent_note_t0[sid]
            if len(ring) == ring.maxlen:
                self._subagent_note_drops[sid] += 1
            ring.append((seq, ts, severity, text))
            # Unread tracking for the CLI footer (issue #67).
            self._unread_notes_total += 1
            self._unread_notes_by_severity[severity] = (
                self._unread_notes_by_severity.get(severity, 0) + 1
            )
            unread_total = self._unread_notes_total
            unread_by_sev = dict(self._unread_notes_by_severity)
        emitter = self._notes_unread_emitter
        if emitter is not None:
            try:
                emitter(unread_total, unread_by_sev)
            except Exception:
                logger.exception("notes_unread emitter raised")
        return seq, ts

    def _clear_subagent_notes(self, sid: str) -> None:
        """Drop a subagent's ring (issue #65 — terminate / pipe close).

        Called when a subagent is terminated or its pipe closes
        unexpectedly. Late peeks of the dead sid return the
        unknown-subagent marker; keeping a ghost ring would risk
        confusing peek with stale history.
        """
        with self._notes_lock:
            self._subagent_notes.pop(sid, None)
            self._subagent_note_seq.pop(sid, None)
            self._subagent_note_drops.pop(sid, None)
            self._subagent_note_t0.pop(sid, None)

    def _drain_pending_async(self) -> int:
        """Append every queued async-subagent reply as a user message.

        Called at the top of each `run` loop iteration so any subagent
        that finished an async call since the last LLM API call has
        its reply waiting for the model on the very next turn. The
        replies are pre-formatted by the IO thread that enqueued them
        (typically `[subagent <name> (<id>) reports]: <text>`).

        Returns the number of replies drained.
        """
        n = 0
        while True:
            try:
                reply = self.pending_async_replies.get_nowait()
            except queue.Empty:
                break
            self.conversation.append({"role": "user", "content": reply})
            n += 1
        # Reset unread-notes counters; the LLM is about to see
        # whatever was queued, including any subagent notes that
        # arrived since the last turn. Emit the zeroed snapshot to
        # the CLI footer (issue #67) only if there were unread
        # notes — avoids spamming the same "0" event every turn.
        with self._notes_lock:
            had_unread = self._unread_notes_total > 0
            self._unread_notes_total = 0
            for k in self._unread_notes_by_severity:
                self._unread_notes_by_severity[k] = 0
        if had_unread:
            emitter = self._notes_unread_emitter
            if emitter is not None:
                try:
                    emitter(0, {"info": 0, "warn": 0, "alert": 0})
                except Exception:
                    logger.exception("notes_unread emitter raised")
        return n

    def run(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        self.conversation.append({"role": "user", "content": prompt})
        texts: list[str] = []
        while True:
            # Pick up any plugin directories that appeared on disk
            # since the last iteration (e.g. one the LLM just authored
            # via the write-plugin skill). Loader notes are pushed
            # onto pending_async_replies so the drain immediately
            # below surfaces them on this same API call.
            if self.plugins is not None:
                try:
                    self.plugins.rescan_for_new(self)
                except Exception:
                    logger.exception("plugin rescan_for_new raised")
            # Drain any async-subagent replies that arrived since the
            # last LLM call so the model sees them on this turn.
            self._drain_pending_async()
            # Rebuild every inner call so a skill installed mid-run
            # shows up on the next iteration. The catalog renders
            # sorted; identical filesystem state ⇒ identical string
            # ⇒ prompt cache stays warm.
            stable, volatile = self._system_prompt_segments()
            turn = self._call_llm(
                self.conversation,
                stable,
                system_volatile=volatile,
                on_text_delta=on_text_delta,
            )
            self.conversation.append(turn)
            # After every assistant turn, evict stale single-shot tool
            # results in-memory (see `_apply_eviction`). Cheap walk;
            # only mutates entries belonging to tools registered with
            # `evict_after_use=True`. JSONL on disk is untouched.
            self._apply_eviction()
            usage = turn.get("usage") if isinstance(turn, dict) else None
            if usage:
                for k in ("input", "output", "cache_creation", "cache_read"):
                    self.token_usage[k] += int(usage.get(k, 0) or 0)
                if on_usage:
                    on_usage(usage)

            if text := turn.get("content", ""):
                texts.append(text)
                if on_text:
                    on_text(text)
                if self.plugins is not None:
                    self.plugins.call_after_assistant_response(text)

            tool_calls = turn.get("tool_calls", [])
            if not tool_calls:
                return "\n\n".join(texts)

            # Always finish the current tool batch before checking
            # cancel — Anthropic / OpenAI both require a tool_result
            # for every tool_use, so partial completion would leave
            # the conversation invalid.
            results = []
            for call in tool_calls:
                content = self._route_tool(
                    call,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
                entry: dict[str, Any] = {
                    "id": call["id"],
                    "name": call["name"],
                    "content": content,
                }
                # `_render_tool_result` set this side channel iff the
                # tool's output was offloaded to a session attachment.
                # Surface as a structured field so audit/replay tools
                # don't have to regex the path out of `content`.
                if self._last_tool_attachment is not None:
                    entry["attachment"] = self._last_tool_attachment
                    self._last_tool_attachment = None
                results.append(entry)
            self.conversation.append({"role": "user", "tool_results": results})
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
