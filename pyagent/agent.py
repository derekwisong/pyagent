import collections
import logging
import queue
import threading
import time
from typing import Any
from collections.abc import Callable

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
        self._evict_after_use: dict[str, bool] = {}
        self._last_tool_attachment: dict[str, Any] | None = None
        self.conversation: list[Any] = []
        self.plugins = plugins
        self.depth: int = depth
        self._subagents: dict[str, Any] = {}
        self.token_usage: dict[str, int] = {
            "input": 0,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
        }
        self.pending_async_replies: queue.Queue = queue.Queue()
        self._subagent_notes: dict[str, collections.deque] = {}
        self._subagent_note_seq: dict[str, int] = {}
        self._subagent_note_drops: dict[str, int] = {}
        self._notes_lock = threading.Lock()
        self._subagent_note_t0: dict[str, float] = {}
        self._unread_notes_total: int = 0
        self._unread_notes_by_severity: dict[str, int] = {
            "info": 0,
            "warn": 0,
            "alert": 0,
        }
        self._notes_unread_emitter: Callable[[int, dict[str, int]], None] | None = None

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

    HARD_OFFLOAD_CEILING = 64_000

    TOOL_ARG_ELIDE_THRESHOLD = 4_000

    SOFT_THRESHOLD_FORCED_TOOLS: frozenset[str] = frozenset({"read_file"})

    def _render_tool_result(
        self,
        name: str,
        result: Any,
        args: dict[str, Any] | None = None,
    ) -> str:
        self._last_tool_attachment = None
        if isinstance(result, Attachment):
            if not self.session:
                if result.inline_text is not None:
                    return result.inline_text
                if result.preview:
                    return result.preview
                return result.content if isinstance(result.content, str) else ""
            path = self.session.write_attachment(name, result.content, result.suffix)
            self._last_tool_attachment = {
                "path": str(path),
                "size_bytes": (
                    len(result.content)
                    if isinstance(result.content, (str, bytes))
                    else 0
                ),
            }
            if result.inline_text is not None:
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
            text = "\n".join(str(item) for item in result)
        else:
            text = str(result)
        if self.session:
            auto = self._auto_offload.get(name, True)
            over_threshold = len(text) > self.session.attachment_threshold
            over_ceiling = len(text) > self.HARD_OFFLOAD_CEILING
            forced_soft = (
                not auto and name in self.SOFT_THRESHOLD_FORCED_TOOLS and over_threshold
            )
            if (auto and over_threshold) or over_ceiling or forced_soft:
                path = self.session.write_attachment(name, text)
                self._last_tool_attachment = {
                    "path": str(path),
                    "size_bytes": len(text),
                }
                preview = text[: self.session.preview_chars]
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
        """Replace any oversized string arg with a short marker."""
        args = call.get("args")
        if not isinstance(args, dict):
            return
        for key, val in list(args.items()):
            if isinstance(val, str) and len(val) > self.TOOL_ARG_ELIDE_THRESHOLD:
                args[key] = (
                    f"<{len(val)} chars elided after tool ran; "
                    f"see the tool result for the outcome>"
                )

    _TOOL_HINTS: dict[str, str] = {
        "execute": (
            "stdout was huge — filter (`grep`/`head`) or redirect to a " "file"
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
        """Render the offload notice the agent sees in place of bytes."""
        # keep keys/order stable: pyagent.sessions_audit._OFFLOAD_RE parses this prefix
        parts = [f"[offload {path}", f"produced {size}c"]
        if cap_chars is not None:
            parts.append(f"cap {cap_chars}c")
        if file_lines is not None and file_lines > 0:
            avg = max(1, size // file_lines) if file_lines else 0
            parts.append(f"file {file_lines} lines, ~{avg}c/line avg")
        header = " | ".join(parts) + "]"

        hint_line: str | None = None
        if next_call_hint:
            hint_line = next_call_hint
        elif tool_name and tool_name in Agent._TOOL_HINTS:
            hint_line = f"[hint: {Agent._TOOL_HINTS[tool_name]}]"
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

        ``before_tool`` runs before ``_execute_tool`` so a
        ``decision="block"`` short-circuits before any permission
        prompt reaches the human.
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
                return content
        is_error = False
        try:
            content = self._execute_tool(name, args)
        except Exception as e:
            logger.exception("tool %s raised", name)
            content = f"Error: {type(e).__name__}: {e}"
            is_error = True
        else:
            from pyagent.tools import is_error_result

            is_error = is_error_result(content)
        if self.plugins is not None:
            after = self.plugins.call_after_tool_call(name, args, content, is_error)
            for note in after.extra_user_messages:
                self.pending_async_replies.put(note)
            if after.replaced:
                content = after.result
        if on_tool_result:
            on_tool_result(name, content)
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
        return bool(msg.get("tool_calls"))

    def _apply_eviction(self) -> int:
        """Replace consumed eviction-flagged tool_result content with stubs.

        Provable-staleness rule: a tool_result entry for a tool
        registered with `evict_after_use=True` is stale once at least
        one later assistant turn in the conversation has produced
        output (text and/or tool_calls). The MOST RECENT such result
        (no following assistant turn yet) is still load-bearing.

        Idempotent. JSONL on disk is not touched.

        Returns the number of result entries newly stubbed.
        """
        if not any(self._evict_after_use.values()):
            return 0

        assistant_with_output: list[int] = [
            i
            for i, msg in enumerate(self.conversation)
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
            if i >= last_with_output:
                continue
            for r in results:
                name = r.get("name")
                if not name or not self._evict_after_use.get(name, False):
                    continue
                stub = self._eviction_stub(name)
                if r.get("content") == stub:
                    continue
                r["content"] = stub
                stubbed += 1
        return stubbed

    _NOTES_RING_MAXLEN = 64

    def _append_subagent_note(
        self, sid: str, severity: str, text: str
    ) -> tuple[int, float]:
        """Append a note to a subagent's per-sid ring.

        Returns the (seq, ts) of the appended note. ts is
        monotonic seconds since the first note for this sid.
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
        """Drop a subagent's ring on terminate or pipe close."""
        with self._notes_lock:
            self._subagent_notes.pop(sid, None)
            self._subagent_note_seq.pop(sid, None)
            self._subagent_note_drops.pop(sid, None)
            self._subagent_note_t0.pop(sid, None)

    def _drain_pending_async(self) -> int:
        """Append every queued async-subagent reply as a user message.

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
            if self.plugins is not None:
                try:
                    self.plugins.rescan_for_new(self)
                except Exception:
                    logger.exception("plugin rescan_for_new raised")
            self._drain_pending_async()
            stable, volatile = self._system_prompt_segments()
            turn = self._call_llm(
                self.conversation,
                stable,
                system_volatile=volatile,
                on_text_delta=on_text_delta,
            )
            self.conversation.append(turn)
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

            # finish the current tool batch before checking cancel: providers require a tool_result for every tool_use
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
                if self._last_tool_attachment is not None:
                    entry["attachment"] = self._last_tool_attachment
                    self._last_tool_attachment = None
                results.append(entry)
            self.conversation.append({"role": "user", "tool_results": results})
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
