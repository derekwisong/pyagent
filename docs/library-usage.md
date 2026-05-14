# Pyagent as a library

Pyagent's `Agent` class is built first for the CLI, but it's also a
clean Python primitive for embedding a tool-using LLM into your own
code. This doc covers the library surfaces beyond the README
quickstart — streaming hooks, sessions, the permissions gate,
structured system prompts, and (if you really want it) loading the
bundled plugin set. A bare `Agent` loads nothing beyond the client
and the tools you register; see the README for what the CLI wires
up on top.

## Minimum viable agent

Type hints + docstring on a Python function become the JSON tool
schema the model sees:

```python
from pyagent import Agent, auto_client

def get_current_temperature(city: str) -> float:
    """Look up the current temperature in a city, in Celsius."""
    return 22.5  # your implementation

agent = Agent(
    client=auto_client(),
    system="You are a friendly weather assistant.",
)
agent.add_tool("get_current_temperature", get_current_temperature)
print(agent.run("How warm is it in Paris right now?"))
```

`agent.run(prompt)` runs the agent loop until the model emits a turn
with no tool calls and returns the concatenated assistant text.
Call it again with another prompt to continue the conversation —
history lives on `agent.conversation` and is reused automatically.

For explicit model selection use `get_client("provider/model")`
(same strings as the CLI's `--model` flag) instead of `auto_client()`.

## Streaming

Pass `on_text_delta` (and friends) to `agent.run(...)` to get
incremental output:

```python
agent.run(
    "What's the weather in Paris and Tokyo?",
    on_text_delta=lambda chunk: print(chunk, end="", flush=True),
    on_tool_call=lambda name, args: print(f"\n[{name}({args})]"),
    on_tool_result=lambda name, content: print(f"[result: {content[:80]}]"),
    on_usage=lambda usage: print(f"\n[tokens: {usage}]"),
)
```

`on_usage` fires after each LLM call with per-call token counts
(`input`, `output`, `cache_creation`, `cache_read`, `model`).
Cumulative usage lives on `agent.token_usage`.

## Sessions (optional persistence)

Without a session the conversation lives only in memory. Pass a
`Session` to persist history and offload large tool results to disk:

```python
from pyagent import Agent, Session, auto_client

session = Session(session_id="my-app-123")
agent = Agent(client=auto_client(), session=session)
agent.conversation = session.load_history()  # resume prior run
agent.run("Continue where we left off.")
```

Tool results larger than `Session.attachment_threshold` get written
to `<session_dir>/attachments/` and replaced in-conversation with a
short reference, so a big log dump doesn't bloat every later LLM call.

## Permissions

Pyagent's built-in file/shell tools call
`permissions.require_access(path)` before touching anything. Default
behavior: paths inside the cwd pass silently; paths outside prompt
on stdin. In a library context with no human at stdin, pre-approve
the paths you need or inject a non-interactive handler:

```python
from pyagent import permissions

permissions.pre_approve("/home/me/data")
permissions.set_prompt_handler(lambda path: True)   # trust everything
# or: permissions.set_prompt_handler(lambda path: False)  # deny outside cwd
```

Custom tools you add via `agent.add_tool()` do not go through the
gate unless you call `permissions.require_access(path)` yourself.

## Structured system prompts

`Agent(system=...)` accepts a plain string or a `SystemPromptBuilder`
for the SOUL/TOOLS/PRIMER + plugin-section layout the CLI uses:

```python
from pathlib import Path
from pyagent import Agent, auto_client
from pyagent.prompts import SystemPromptBuilder

builder = SystemPromptBuilder(
    soul=Path("my-soul.md"),
    tools=Path("my-tools.md"),
    primer=Path("my-primer.md"),
)
agent = Agent(client=auto_client(), system=builder)
```

The builder matters when you want plugin-contributed prompt sections
(memory ledger, skills catalog, etc.) — pass `plugin_loader=loaded`.

## Loading the bundled plugins and skills

The CLI mounts both for you. For the snippets to wire `pyagent.plugins`
and `pyagent.skills` into a library `Agent`, see
[plugins.md → Using bundled plugins in your Agent](plugins.md#using-bundled-plugins-in-your-agent)
and
[skills.md → Using skills in your Agent](skills.md#using-skills-in-your-agent).

If you find yourself reassembling much more than that, consider
invoking the CLI as a subprocess instead.
