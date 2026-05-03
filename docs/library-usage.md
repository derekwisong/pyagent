# Pyagent as a library

Pyagent's `Agent` class is built first for the CLI, but it's also a
clean Python primitive for embedding a tool-using LLM into your own
code. This doc covers the surfaces beyond the README quickstart.

The CLI is one specific consumer of `Agent` (in `pyagent/cli.py` →
`pyagent/agent_proc.py`). It wires up sessions, plugins, subagents,
and the prompt-toolkit UI. Library use lets you pick the pieces you
want.

## What's loaded by default

Bare `Agent(client=..., system="...")` is genuinely bare. Nothing
auto-loads beyond what you pass in:

| Thing | Loaded by default? |
|---|---|
| Your custom tools (via `agent.add_tool(...)`) | ✓ whatever you add |
| LLM client (passed to `Agent(client=...)`) | ✓ |
| Plugins — `web_search`, `doc_tools`, `memory_markdown`, etc. | ✗ |
| Skills catalog + `read_skill` tool | ✗ |
| Built-in file/shell tools (`read_file`, `write_file`, `execute`, `grep`, `fetch_url`, …) | ✗ |
| Subagent meta-tools (`spawn_subagent`, `call_subagent`, …) | ✗ |
| Memory ledgers (USER.md, MEMORY.md) | ✗ |
| Permissions framework | available as a module; no tool calls in unless you wire it |

The CLI loads all of that because `pyagent/agent_proc.py:_bootstrap`
calls `plugins.load()`, `_register_tools`, etc. The library
deliberately doesn't — embedding a tool-using LLM in your own app
typically wants explicit scope ("here are the four tools I expose to
the model"), not auto-loaded subsystems.

Each of the rows in the "✗" half is opt-in below.

## The minimum viable agent

Type hints + docstring on a Python function become the JSON tool
schema the model sees:

```python
from pyagent import Agent, auto_client

def get_current_temperature(city: str) -> float:
    """Look up the current temperature in a city, in Celsius."""
    # ... your implementation ...
    return 22.5

agent = Agent(
    client=auto_client(),
    system="You are a friendly weather assistant.",
)
agent.add_tool("get_current_temperature", get_current_temperature)
reply = agent.run("How warm is it in Paris right now?")
print(reply)
```

`agent.run(prompt)` runs the agent loop to terminal (i.e. until the
model emits a turn with no tool calls) and returns the concatenated
assistant text from all turns.

## Streaming and observability

Pass callbacks to `agent.run(...)` to get incremental text and
tool-call events as they happen:

```python
def on_text_delta(chunk: str) -> None:
    print(chunk, end="", flush=True)

def on_tool_call(name: str, args: dict) -> None:
    print(f"\n[calling {name}({args})]")

def on_tool_result(name: str, content: str) -> None:
    print(f"[result: {content[:80]}...]")

def on_usage(usage: dict) -> None:
    print(f"\n[tokens: {usage}]")

agent.run(
    "What's the weather in Paris and Tokyo?",
    on_text_delta=on_text_delta,
    on_tool_call=on_tool_call,
    on_tool_result=on_tool_result,
    on_usage=on_usage,
)
```

The `on_text_delta` callback fires per-chunk during streaming; the
final assistant text is also returned from `run()`. `on_usage` fires
after each LLM call with the per-call token-usage dict (`input`,
`output`, `cache_creation`, `cache_read`, `model`).

Cumulative usage across the agent's lifetime is on
`agent.token_usage`.

## Multi-turn conversations

`agent.run(prompt)` appends to `agent.conversation` and reuses it on
the next call. Just call `run` again with the next user prompt:

```python
agent.run("Multiply 7 by 6.")
agent.run("Now divide by 3.")            # remembers the previous result
agent.run("What did I ask you first?")   # remembers the original question
```

If you want a fresh start without re-creating the agent, clear it:

```python
agent.conversation = []
agent.token_usage = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0}
```

## Picking a model explicitly

`auto_client()` picks based on env-vars; for explicit selection use
`get_client("provider/model")`:

```python
from pyagent import Agent, get_client

# Anthropic, specific model
agent = Agent(client=get_client("anthropic/claude-opus-4-7"), system="...")

# OpenAI's cheap variant
agent = Agent(client=get_client("openai/gpt-4o-mini"), system="...")

# Local Ollama (plugin-registered provider, requires the daemon running)
agent = Agent(client=get_client("ollama/llama3.2:latest"), system="...")
```

Provider/model strings match the CLI's `--model` flag, including
plugin-registered providers like `ollama` once the plugin is loaded.

## Sessions (optional persistence)

Constructing an `Agent` without a session works fine — the
conversation lives only in memory. For persistence (replay across
process restarts, attachment offload of large tool results), pass a
`Session`:

```python
from pyagent import Agent, Session, auto_client

session = Session(session_id="my-app-123")  # creates .pyagent/sessions/my-app-123/
agent = Agent(client=auto_client(), session=session)

# Resume a prior session: load history, then run normally
agent.conversation = session.load_history()
agent.run("Continue where we left off.")
```

Sessions also enable the **attachment-offload** path: tool results
over `Session.attachment_threshold` bytes get written to
`<session_dir>/attachments/` and replaced in-conversation with a
short reference, so a 50KB log dump doesn't bloat every subsequent
LLM call.

`Session` includes per-session attachment-dir LRU eviction (default
25 MB cap) — see `pyagent.config.resolve_attachment_dir_cap_mb`
for tuning.

## Tools that touch files: the permissions gate

Pyagent's built-in tools (`pyagent.tools.read_file`, `write_file`,
`execute`, etc.) call `permissions.require_access(path)` before
touching anything. The default behavior:

- Path inside the current working directory: silent allow.
- Path outside: prompts on stdin for `[y]es / [a]lways / [n]o`.

In a library context with no human at stdin, the prompt hangs or
fails. Three workarounds depending on your trust model:

```python
from pyagent import permissions

# (a) Pre-approve specific paths your agent should reach.
permissions.pre_approve("/home/me/data")
permissions.pre_approve("/tmp/agent-workspace")

# (b) Allow everything (DANGEROUS — only for trusted environments).
permissions.set_prompt_handler(lambda path: True)

# (c) Deny everything outside cwd (strictest).
permissions.set_prompt_handler(lambda path: False)
```

Custom tools you add via `agent.add_tool()` **do not** go through
the permission gate by default — only pyagent's built-in primitives
do. If you write your own file-touching tool and want it gated, call
`permissions.require_access(path)` from inside it.

## System-prompt customization

The `system` argument to `Agent(...)` accepts either a string (used
verbatim) or a `SystemPromptBuilder` instance for the structured
SOUL/TOOLS/PRIMER + plugin-section render path the CLI uses:

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

For most library use the plain-string form is enough. The builder
matters when you want plugin-contributed prompt sections (memory,
skills catalog, etc.) — see "Attaching plugins" below.

## Adding built-in tools (file/shell primitives)

If you want the model to be able to read files, run shell commands,
or use the other built-in primitives — but don't want the full
plugin set — register them individually:

```python
from pyagent import Agent, auto_client, permissions
from pyagent.tools import read_file, write_file, edit_file, grep, execute

# Pre-approve the directory tree the agent should reach so the
# permission gate doesn't prompt stdin in a library context.
permissions.pre_approve("/path/to/your/workspace")

agent = Agent(client=auto_client(), system="You are a code-reading assistant.")
agent.add_tool("read_file", read_file, auto_offload=False)
agent.add_tool("grep", grep)
agent.add_tool("execute", execute)
agent.run("Find every TODO in src/, then summarize.")
```

Common built-ins worth knowing about:

| Tool | What it does |
|---|---|
| `read_file(path, start=None, end=None)` | Read a file, optionally a line range. Returns string contents or an offloaded reference. |
| `write_file(path, content, append=False)` | Write or append a file. |
| `edit_file(path, old_string, new_string, replace_all=False)` | Exact-match diff edit. |
| `grep(pattern, path, before=N, after=N, context=N)` | Regex search with optional context lines. |
| `glob(pattern, root=".", limit=200)` | Recursive name match. |
| `list_directory(path)` | Single-level directory listing. |
| `execute(command)` | One-shot shell. 60s timeout. |
| `run_background(command, name="...")` / `read_output` / `wait_for` / `kill_process` | Long-running shell quartet. |
| `fetch_url(url, format="md")` | HTTP GET, returns markdown of HTML pages by default. |

All of them go through `permissions.require_access(path)` for any
filesystem path they touch. Use `permissions.pre_approve(path)` or
`permissions.set_prompt_handler(callable)` from the section above.

`read_file` is registered with `auto_offload=False` in the CLI
because callers slice ranges intentionally — pass the same kwarg in
the library to match.

## Attaching the bundled plugins (optional)

If you want the full plugin surface (memory ledger, web search,
doc-tools, etc.) in a library-mode agent, load the plugin set the
same way the CLI does:

```python
from pyagent import Agent, auto_client
from pyagent import plugins as plugins_mod
from pyagent.prompts import SystemPromptBuilder
from pyagent.session import Session

session = Session()
loaded = plugins_mod.load()
loaded.bind_session(session)

builder = SystemPromptBuilder(
    soul="...",          # or Path(...)
    tools="...",
    primer="...",
    plugin_loader=loaded,
)

agent = Agent(
    client=auto_client(),
    system=builder,
    session=session,
    plugins=loaded,
)
loaded.bind_agent(agent)

# Plugin tools are now in loaded.tools(); register them on the agent.
for name, (_plugin, fn) in loaded.tools().items():
    agent.add_tool(name, fn)
```

You probably don't want this for an embedded use — it pulls in the
full pyagent feature set (subagent registry, ledgers, attachment
offload). For most library users, custom tools + bare Agent is the
right shape.

## What lives where

Quick reference for the surfaces you'll touch:

| Module | Use for |
|---|---|
| `pyagent.Agent` | The main loop. |
| `pyagent.auto_client` / `pyagent.get_client` | LLM client construction. |
| `pyagent.LLMClient` | Protocol type for typing custom clients. |
| `pyagent.Session` | Persistent conversation / attachment offload. |
| `pyagent.Attachment` | Return type for tools that emit large or structured side data. |
| `pyagent.permissions` | Path-access gate. |
| `pyagent.tools` | Built-in tool implementations (read/write/execute/grep/...). |
| `pyagent.prompts.SystemPromptBuilder` | Structured system-prompt assembly. |
| `pyagent.plugins` | Plugin loader and `LoadedPlugins` registry. |

## Limits of the library use case

The CLI does some things that are tricky to replicate in library
use:

- **Mid-turn cancel** (Ctrl-C / Esc) requires a threading.Event you
  pass via `cancel_event=`; the CLI wires this up to its UI. In
  library code you'd manage that yourself.
- **Subagents** spawn via `multiprocessing.spawn` and require the
  CLI's child-process bootstrap. Library use can register subagent
  tools but the orchestration in `agent_proc.py` is closely tied to
  the CLI shape.
- **Skills + roles** are surfaced through the CLI's bootstrap. The
  primitives are reachable from the library but require more
  manual wiring than is worth it for most embedded uses.

If you find yourself reaching for these from the library, consider
whether what you actually want is to invoke the CLI as a subprocess
(`subprocess.run(["pyagent", "--prompt", "..."])`) rather than
re-assemble the harness in-process.
