"""IPC protocol between the CLI process and an agent subprocess (and
between an agent process and any subagents it spawns).

Events flow over a duplex `multiprocessing.Connection`. Payloads are
plain dicts; the default Connection codec (pickle) handles serialization.

Subagent annotation
-------------------
When an agent process forwards an event from one of its subagents
upstream, it adds an `agent_id` key whose value is the subagent's id.
Events without `agent_id` belong to "this" agent. The CLI uses this
to label rendered events (e.g. `[name]` prefix). The deepest agent
that originated the event sets `agent_id`; intermediate forwarders
preserve it.

For events the CLI sends *to* a subagent (currently just
`permission_response`), the CLI sets `agent_id` to the targeted
subagent and the parent agent's IO thread routes it down the right
pipe.

CLI → agent
  - user_prompt {prompt: str, persist: bool = True}
        Run a turn. If persist is False, the child does not append the
        resulting conversation entries to its session (used for the
        end-of-session memory pass, which mutates ledgers but should
        not appear in the saved transcript).
  - cancel {}
        Set the child's cancel_event AND propagate to all subagents.
        The current tool batch finishes, then `agent.run` raises
        KeyboardInterrupt at its safe point.
  - permission_response {decision: bool, always: bool, agent_id?: str}
        Reply to a prior `permission_request`. `always` causes the
        target agent to cache the path in `permissions.pre_approve`
        so the same path is not re-prompted. If `agent_id` is set,
        the parent agent forwards the response down to that subagent.
  - shutdown {}
        Clean exit. Child terminates any subagents it owns, closes its
        connection, and returns.

agent → CLI
  - ready {}
        Sent once after the child has finished bootstrapping its
        Agent, Session, and tool registry. The CLI waits for this
        before accepting user input.
  - assistant_text {text: str}
        Streamed assistant text from a turn (currently emitted whole
        per turn, not chunked).
  - tool_call_started {name: str, args: dict}
        Mirror of the existing `on_tool_call` callback.
  - tool_result {name: str, content: str}
        Mirror of the existing `on_tool_result` callback.
  - permission_request {target: str}
        The child's permissions layer needs an interactive y/n/a
        decision. The child blocks until a `permission_response`
        arrives.
  - info {level: str, message: str}
        Free-form status (orphan-attachment purges, deprecation
        notices, etc.). `level` is "info" or "warn".
  - usage {input: int, output: int}
        Per-LLM-call token deltas. Emitted from `Agent.run`'s
        `on_usage` callback after each provider response. The CLI
        accumulates these per-agent in its `agents_state` dict and
        renders the running total (and a USD cost estimate when
        the model is in the pricing table) in the status footer.
  - checklist {tasks: list[dict]}
        Snapshot of the agent's current task list. Emitted after
        every add_task / update_task call. `tasks` is a list of
        `{id, title, status, note}` dicts in insertion order. The
        CLI uses it to render a progress segment in the status footer
        and to print full state on `/tasks`.
  - turn_complete {final_text: str}
        The child finished a `user_prompt` (no errors). `final_text`
        is the aggregated assistant text returned by `agent.run` and
        is the value `call_subagent` returns to the parent agent's
        tool result. The CLI may send the next user_prompt.
  - agent_error {kind: str, message: str, fatal: bool = False}
        The child's turn ended in an exception or cancellation. If
        `fatal` is True, the child is exiting (e.g. unrecoverable
        bootstrap failure) and the CLI should clean up. Otherwise
        the child has rolled back any unsaved conversation entries
        and the CLI may send the next user_prompt.
"""

from __future__ import annotations

from multiprocessing.connection import Connection
from typing import Any


def send(conn: Connection, event_type: str, **payload: Any) -> None:
    """Send a typed event over `conn`. Payload keys must be picklable."""
    conn.send({"type": event_type, **payload})


def recv(conn: Connection) -> dict[str, Any]:
    """Receive one event dict from `conn`. Raises EOFError on close."""
    return conn.recv()
