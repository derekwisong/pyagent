import logging
import queue
import threading
from typing import Any, Callable

from pyagent.llms import LLMClient
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
    ) -> None:
        self.client = client
        self.system = system
        self.session = session
        self.tools: dict[str, Callable[..., Any]] = {}
        self._auto_offload: dict[str, bool] = {}
        self.conversation: list[Any] = []
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
        self.token_usage: dict[str, int] = {"input": 0, "output": 0}
        # Async subagent inbox. The IO thread (in agent_proc) puts
        # formatted reply strings here when an async-fired subagent
        # finishes its turn. `_drain_pending_async`, called at the
        # top of each `run` loop iteration, appends them to
        # `conversation` as user-role messages so the LLM sees
        # them on its very next API call. Thread-safe queue —
        # the IO thread is what produces, the main thread (where
        # run() executes) is what consumes.
        self.pending_async_replies: queue.Queue = queue.Queue()

    def add_tool(
        self,
        name: str,
        fn: Callable[..., Any],
        auto_offload: bool = True,
    ) -> None:
        self.tools[name] = fn
        self._auto_offload[name] = auto_offload

    def _tool_schemas(self) -> list[dict[str, Any]]:
        """Build JSON-schema tool definitions from each tool's type hints + docstring."""
        return [schema(name, fn) for name, fn in self.tools.items()]

    def _system_prompt(self) -> str | None:
        if isinstance(self.system, SystemPromptBuilder):
            return self.system.build()
        return self.system

    def _call_llm(self, messages: list[Any], system: str | None) -> Any:
        """One model turn. Sends conversation + tool schemas, returns the assistant turn."""
        return self.client.respond(
            conversation=messages,
            system=system,
            tools=self._tool_schemas(),
        )

    def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        result = self.tools[name](**args)
        return self._render_tool_result(name, result)

    # Hard ceiling: any tool result above this size is offloaded
    # regardless of the tool's `auto_offload` setting. Prevents a
    # runaway read_file (auto_offload=False but actual file is huge)
    # from blowing the request size on the next turn.
    HARD_OFFLOAD_CEILING = 64_000

    def _render_tool_result(self, name: str, result: Any) -> str:
        if isinstance(result, Attachment):
            if not self.session:
                if result.preview:
                    return result.preview
                return result.content if isinstance(result.content, str) else ""
            path = self.session.write_attachment(
                name, result.content, result.suffix
            )
            return self._format_offload_ref(
                path, len(result.content), result.preview
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
            if (auto and over_threshold) or over_ceiling:
                path = self.session.write_attachment(name, text)
                preview = text[: self.session.preview_chars]
                return self._format_offload_ref(path, len(text), preview)
        return text

    @staticmethod
    def _format_offload_ref(path: Any, size: int, preview: str) -> str:
        header = (
            f"[output saved to {path} ({size} chars) — preview below. "
            f"Do NOT read_file the whole attachment; that pulls every "
            f"byte back into context permanently. If you need more, "
            f"grep for what you want or read_file with start/end to "
            f"slice a specific range.]"
        )
        if preview:
            return f"{header}\n\n--- preview ---\n{preview}"
        return header

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
        """
        name = call["name"]
        args = call["args"]
        if on_tool_call:
            on_tool_call(name, args)
        try:
            content = self._execute_tool(name, args)
        except Exception as e:
            logger.exception("tool %s raised", name)
            content = f"Error: {type(e).__name__}: {e}"
        if on_tool_result:
            on_tool_result(name, content)
        return content

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
        return n

    def run(
        self,
        prompt: str,
        on_text: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_result: Callable[[str, str], None] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        self.conversation.append({"role": "user", "content": prompt})
        texts: list[str] = []
        while True:
            # Drain any async-subagent replies that arrived since the
            # last LLM call so the model sees them on this turn.
            self._drain_pending_async()
            # Rebuild every inner call so a skill installed mid-run
            # shows up on the next iteration. The catalog renders
            # sorted; identical filesystem state ⇒ identical string
            # ⇒ prompt cache stays warm.
            system = self._system_prompt()
            turn = self._call_llm(self.conversation, system)
            self.conversation.append(turn)
            usage = turn.get("usage") if isinstance(turn, dict) else None
            if usage:
                self.token_usage["input"] += int(usage.get("input", 0) or 0)
                self.token_usage["output"] += int(usage.get("output", 0) or 0)
                if on_usage:
                    on_usage(usage)

            if text := turn.get("text", ""):
                texts.append(text)
                if on_text:
                    on_text(text)

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
                results.append(
                    {"id": call["id"], "name": call["name"], "content": content}
                )
            self.conversation.append({"role": "user", "tool_results": results})
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
