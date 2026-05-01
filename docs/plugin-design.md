# Pyagent Plugin System — Design

Status: v3 proposal after three review rounds (distsys, AI engineering,
software engineering). Companion docs: `plugin-feature-summary.md`,
`plugin-competitive-landscape.md`, `plugin-memory-migration.md`.

## Goals

- **Simple but flexible.** A working plugin fits in ~80 lines of
  Python. No subclassing, no DI container.
- **Dogfood.** Pyagent's own memory subsystem becomes the first
  plugin. The bundled `memory-markdown` exercises the entire v1 API
  surface — if the API can't express it cleanly, the API is wrong.
- **Effective arms for the agent.** The agent can author plugins. The
  hook surface is sized so a plugin built by the LLM can do meaningful
  work: extract facts from assistant turns, observe tool calls, persist
  memory.
- **Small blast radius.** A bad plugin should fail loudly and stay out
  of the agent's way. v1 doesn't promise enforcement Python can't
  deliver — see "Hook timing and enforcement" below.

## Non-goals (v1)

- Sandboxing untrusted plugins. Plugins run in-process; treat them
  like dependencies you `pip install`.
- LLM provider plugins. Reserved name; out of scope.
- Channels (telegram, discord). Reserved name; out of scope.
- Plugin-defined slash commands. Defer.
- Capability/permission negotiation. Out.
- Hot reload. Plugin changes require an agent process restart.
- Plugin-spawned isolated agents (`api.create_agent`). Reserved for
  v2; see "Plugin runtime vision."
- Asynchronous notifications back to the user (`api.deliver`,
  `api.ask_user`). Reserved for v2.

## Concepts

| Concept | Lives where | Role |
| --- | --- | --- |
| **Tool** | `pyagent/tools.py`, plugin code | Python function the LLM can call. |
| **Skill** | `<config-dir>/skills/<name>/SKILL.md`, etc. | Markdown the agent loads on demand. Passive. |
| **Plugin** | `<config-dir>/plugins/<name>/` or installed Python package | Active code that registers tools, contributes prompt text, observes the conversation loop. |
| **Role** | `[models.<name>]` in `config.toml` | Named subagent preset (model + tools + prompt). |
| **Provider** | `pyagent/llms/*.py` | LLM client. Out of plugin scope. |

Roles configure subagents; plugins extend the agent process. The two
don't conflict because they target different scopes.

### Vocabulary

- **Session** — the `Session` object in `pyagent/session.py`. Owns
  the conversation history under `.pyagent/sessions/<id>/`. One
  pyagent invocation hosts exactly one session.
- **Terminal** — the rendered output stream the human reads. Includes
  things that aren't in the session: `info` events, status footer,
  tool-call previews. `api.log(...)` writes here.
- **The human** — the actor at the keyboard.
- **Observer hook** — a hook whose return value pyagent ignores. **All
  v1 hooks are observers.** Controlling hooks (return value directs
  flow) are reserved for v2.

## The plugin contract

A plugin is a Python module exposing a top-level `register` function:

```python
def register(api: PluginAPI) -> None:
    ...
```

`api` is the **only** seam between plugin code and pyagent internals.
Plugins must not import from `pyagent.agent`, `pyagent.agent_proc`,
`pyagent.session`, `pyagent.subagent`, or `pyagent.tools` — these are
unstable. Importing `pyagent.paths` is allowed; that surface is
minimal and well-defined.

Alongside the module, the plugin ships a `manifest.toml`. The manifest
is metadata only — pyagent reads and validates it without executing
plugin code, so a malformed manifest never crashes the loader.

### Manifest schema

```toml
name = "memory-markdown"           # globally unique
version = "0.1.0"                  # plugin's own version
description = "Markdown-file memory backend (the original ledger system)."
api_version = "1"                  # pyagent plugin API version

# What the plugin promises to register. Validated at load time:
# pyagent fails the plugin loud if register() registers anything not
# listed, or fails to register everything listed. This static surface
# also powers the rich missing-tool error — when the LLM calls a tool
# that's gone, pyagent can name the plugin that provided it.
[provides]
tools = ["read_ledger", "write_ledger"]
prompt_sections = ["memory-guidance"]

# Optional eligibility — plugin is skipped (logged) if any fail.
[requires]
python = ">=3.11"
env = []                           # required env vars
binaries = []                      # required CLI binaries on PATH

# Spawn-tree behavior. Default true: plugin loads in every agent
# process, including subagents. Set false for plugins that aren't
# parallel-safe — they only load in the root agent.
[load]
in_subagents = true
```

There is no `kind` field, no `[capabilities]` block. Both were dropped
after review — neither was enforced and both invited rot.

### The PluginAPI surface (v1)

**13 elements total.** The example plugin (`memory-markdown`) uses
all of them.

Read-only attributes (5):

```python
api.config_dir       # Path: <config-dir>
api.workspace        # Path: cwd at agent startup
api.user_data_dir    # Path: <data-dir>/plugins/<name>/  — lazy-created
api.plugin_config    # dict: this plugin's [plugins.<name>] table
api.plugin_name      # str: this plugin's name
```

Registration (2, called inside `register(api)`):

```python
api.register_tool(name: str, fn: Callable) -> None
"""Add an LLM tool. Tool name must be unique; soft-fail on conflict
(see Error handling)."""

api.register_prompt_section(
    name: str,
    renderer: Callable[[PromptContext], str],
    *,
    volatile: bool = False,
) -> None
"""Provide a function that returns markdown to inject into the system
prompt. `name` must be unique across all plugins (soft-fail on
conflict, same as tool names) and must appear in the manifest's
[provides] prompt_sections list.

The renderer receives a PromptContext giving it read-only access to
recent conversation turns.

If volatile=False (default), the section is treated as stable and
lives inside the prompt-cache breakpoint; pyagent calls the renderer
once per turn but caches based on its output. If volatile=True, the
section lives AFTER the last cache_control marker — its content can
change every turn without invalidating the cached system block. Use
volatile=True for anything that depends on recent conversation
(recently-recalled memories, time-of-day, etc.).

A plugin may register multiple prompt sections — e.g. one stable
section for instructional prose and another that auto-loads a data
file the LLM should always see."""
```

Lifecycle hooks (2):

```python
api.on_session_start(fn: Callable[[Session], None])
"""Fired once after the agent has signaled 'ready' upstream and the
IO thread is running. Plugin can warm caches, validate config,
migrate on-disk schema, seed default files. The agent does not
dequeue user prompts until all plugins' on_session_start callbacks
have returned (see "Startup ordering")."""

api.on_session_end(fn: Callable[[Session], None])
"""Best-effort, fired on clean shutdown. Won't run on SIGKILL.
Don't put durability-critical work here — persist incrementally
inside tool calls or after_assistant_response instead."""
```

Observation hooks (3):

```python
api.after_assistant_response(fn: Callable[[str], None])
"""Fired once after each LLM turn that produced text, with the
concatenated text as the argument. A turn with only tool calls and
no text doesn't fire it. Plugin can extract facts, index for vector
recall, persist a clean transcript, trigger external systems, etc.
Observer only — return value ignored."""

api.before_tool_call(fn: Callable[[str, dict], None])
"""Fired before each tool call. Plugin can log, audit, prefetch
context. Observer only — cannot abort or modify the call (controlling
hooks are v2)."""

api.after_tool_call(fn: Callable[[str, dict, str], None])
"""Fired after each tool call with (name, args, result). Plugin can
extract entities, update working memory, etc. Observer only."""
```

Utility (1):

```python
api.log(level: str, message: str)
"""Emit a structured log line tagged with the plugin name. Levels:
'debug', 'info', 'warn', 'error'. Goes through the same event stream
as info events from the agent — the human sees plugin output."""
```

### PromptContext

Passed to renderers (volatile or not — same signature). One field:

```python
class PromptContext:
    recent_messages: tuple[Message, ...]   # read-only view of last 8 turns
```

A vector-recall plugin reads `recent_messages[-1]` to know what the
user just asked, embeds it, retrieves matches, returns markdown.
Plugins that don't need conversation context (like `memory-markdown`'s
static instructional prose) just ignore it.

`turn_count`, `model`, `session_id` were considered and cut — no
plugin shape we modeled needed them. Adding fields later is additive
and doesn't break the contract.

## Hook timing and enforcement

**Plugins should not block the agent loop.** If a plugin needs to do
slow work — embedding, network calls, database writes — it should
defer it (background task, deferred persistence) rather than block in
the hook callback. The result will land in next turn's render, not
this turn's.

A plugin that hangs in a hook hangs the agent. Same blast radius as a
tool that hangs. v1 does not impose deadlines on hook callbacks
because Python cannot reliably preempt running code without leaking
threads or breaking C-extension calls. The honest contract is:

- Pyagent calls the hook on the main thread, in order, synchronously.
- The hook runs to completion.
- If it takes too long, the agent loop waits.
- If it raises, pyagent catches and logs (the agent loop continues).

Plugin authors who want their plugin to behave well under load should
write fast handlers and use **fire-and-forget patterns** for slow
work: queue a background task that writes to disk, and let the next
turn's renderer pick up the persisted result. A vector-recall plugin's
canonical pattern is:

```
after_assistant_response:  embed + index the assistant text, persist
register_prompt_section (volatile):  read pre-computed embeddings,
                                     retrieve, return markdown
```

This means recall is **always one turn stale** — the plugin retrieves
based on what it indexed up to the previous turn. That's the only
shape that fits Python's no-preemption reality.

## Cache-breakpoint architecture

Pyagent currently puts one `cache_control: ephemeral` marker at the
end of the system prompt; any byte-level change invalidates the entire
cached span. A naive memory plugin that updates "recently relevant
memories" each turn would silently wreck cache hit rate.

**Resolution:**

- The `volatile` flag on `register_prompt_section` controls cache placement:
  - `volatile=False` — section lives inside the cached span.
  - `volatile=True` — section lives **after** the last `cache_control`
    marker (or as a synthetic leading user-role message immediately
    before the actual user turn). Changes turn-to-turn without
    invalidating the cached span.
- Pyagent uses up to 4 `cache_control` markers per Anthropic API
  request (the API's allowance). The exact placement is internal to
  `pyagent/prompts.py` and each LLM client; plugins only see
  `volatile`.
- Renderer non-determinism: `volatile=False` renderers must be **pure
  functions** of `PromptContext`. A renderer that reads a clock or a
  file mtime busts the cache silently. Document loud; consider a
  hash-and-warn check.

The tool catalog itself is **stable for the agent process lifetime**
(plugins load once at bootstrap, no mid-session enable/disable). So
tool-schema bytes don't shift between turns and the cache stays warm
across the run.

## Lifecycle

```
discover ─┬─ validate manifest ─ load module ─ register(api) ─┐
          │  (including [provides])                            │
          │                                                    ▼
          └─ on bad manifest, log+skip                state.send("ready")
                                                              │
                                                              ▼
                                                     io_thread.start()
                                                              │
                                                              ▼
                                              on_session_start  (sequential, all plugins)
                                                              │
                                                              ▼
                          ─────── agent loop ───────────────────
                          turn:                                │
                            renderer(ctx)                      │
                            LLM call                           │
                            after_assistant_response                  │
                            before_tool_call                   │
                            tool runs                          │
                            after_tool_call                    │
                          ───────────────────────────────────────
                                                              │
                                                              ▼
                                                       on_session_end
```

### Startup ordering

- `register(api)` runs during bootstrap.
- `state.send("ready")` notifies the parent. The parent considers the
  agent live; the IO thread can route `cancel` / `set_model` / etc.
- `io_thread.start()`.
- `on_session_start` fires for each plugin sequentially. **The
  agent does not dequeue from `work_queue` until every plugin's
  `on_session_start` has returned.** A misbehaving plugin can hang
  startup; the user's only recourse is Ctrl+C the CLI which kills the
  process. Acceptable — same blast radius as a hung tool today.

This ordering closes the round-1 race (where `on_session_start` ran
during bootstrap, before `ready`, with no observer for `cancel`) and
the round-2 race (where `ready` lied because plugin init wasn't
done).

## Discovery

Three tiers, in load order. **Earlier tiers lose** to later — same
precedence as skills.

1. **Bundled** — `pyagent/plugins/<name>/`. Filtered against
   `built_in_plugins_enabled` in `config.toml`.
2. **Entry points** — packages installed in pyagent's Python
   environment that declare
   `[project.entry-points."pyagent.plugins"]`. Discovered via
   `importlib.metadata`.
3. **Drop-ins** — `<config-dir>/plugins/<name>/` and
   `./.pyagent/plugins/<name>/`. Each contains `manifest.toml` and
   `plugin.py` (plus optional helper modules and data files; see
   "Plugin packaging" below). Project beats user beats bundled.

`pyagent-plugins list` shows tier and flags shadowing — a stale
drop-in masking a fresh `pip install` is visible, not silent.

### Load order within a tier

Within a single tier, plugins load in **sorted directory-name order**.
The manifest's `name` field is the plugin's *identity* (used by config
and conflict resolution); the directory name is *disk layout*. They
can differ.

This means users can prefix directory names with numeric ordinals to
control load order:

```
~/.config/pyagent/plugins/
    01-memory-vector/      manifest says name = "memory-vector"
    02-memory-markdown/    manifest says name = "memory-markdown"
    99-experimental/       manifest says name = "my-hack"
```

Same pattern as `init.d`, `conf.d`, `etc/profile.d`. Combined with
the soft-fail tool-conflict rule (first registration wins), this gives
users a deterministic way to resolve conflicts: rename the directory
to influence load order, the manifest name stays put.

### Plugin packaging — multi-file plugins, helpers, data files

A plugin's directory **is a Python package**. `plugin.py` is the
entrypoint that exports `register(api)`; helper modules and data
files live alongside.

```
my-plugin/
    manifest.toml
    plugin.py              # def register(api): ...
    extraction.py          # helper module
    embeddings/
        __init__.py
        client.py
        cache.py
    defaults/
        PROMPT.md
        seed.json
```

In `plugin.py`:

```python
from . import extraction              # helper module
from .embeddings import client        # subpackage

from pathlib import Path
TEMPLATE = (Path(__file__).parent / "defaults" / "PROMPT.md").read_text()
```

The drop-in plugin loader uses `importlib.util.spec_from_file_location`
with `submodule_search_locations=[plugin_dir]` so `from . import ...`
resolves to siblings of `plugin.py`. Entry-point-installed plugins are
already proper Python packages — relative imports work natively with
no special handling.

The bundled `memory-markdown` plugin demonstrates the data-file
pattern via its `defaults/PROMPT.md`, `MEMORY.md`, and `USER.md` seed
templates, accessed through `Path(__file__).parent / "defaults"`.

#### `__init__.py` — when needed

| Context | Top-level `__init__.py`? |
| --- | --- |
| **Drop-in plugin** (`<config-dir>/plugins/foo/`, `./.pyagent/plugins/foo/`) | **No.** `plugin.py` is the entrypoint. The synthetic-spec loader doesn't need one, and drop-in directory names often contain hyphens or numeric prefixes that aren't valid Python identifiers anyway. |
| **Entry-point installed plugin** (`pip install pyagent-foo`) | **Yes.** Real Python package; required by Python itself. The entry point in the package's `pyproject.toml` points at wherever `register` lives. |
| **Bundled plugin** (`pyagent/plugins/foo/`) | **Yes.** Real submodule of the `pyagent` package; imported as `pyagent.plugins.foo`. |

**Subdirectories** that the plugin treats as Python subpackages
(`embeddings/`, `extractors/`, etc.) always need their own `__init__.py`
— that's a standard Python rule, unrelated to pyagent's loader.

If a drop-in author adds a top-level `__init__.py` anyway, pyagent
ignores it. `plugin.py` is the canonical entrypoint; pyagent does not
support two competing conventions.

## Spawn-tree behavior

Plugins re-bootstrap independently per agent process — root and each
subagent each load their own plugin instances.

- **No shared in-memory state across the spawn tree.** `memory-markdown`'s
  in-memory cache in the root is not visible to any subagent.
- **On-disk coordination is the plugin's job.** A plugin that persists
  state must handle concurrent access (file locks, sqlite WAL, etc.).
  Pyagent doesn't provide a lock primitive.
- **`[load] in_subagents = false`** opts out — only the root agent
  loads the plugin. Recommended for plugins doing notification or
  user-facing prompt contributions, since `api.deliver(...)` (v2) from
  a subagent would target the subagent's scratch session, not the
  user's.
- **No plugin object survives a spawn boundary.** `multiprocessing.spawn`
  pickles the agent config dict; pyagent refuses at registration time
  to put any plugin-side object into that dict.

## Configuration

```toml
# Replaces the bundled-plugin default list.
built_in_plugins_enabled = ["memory-markdown"]

[plugins.memory-markdown]
# memory-markdown takes no options today

[plugins.memory-vector]
backend = "lancedb"
embedding_model = "text-embedding-3-small"
enabled = true   # explicit disable for entry-point plugins
```

Plugins not present in `[plugins.<name>]` get `plugin_config = {}` —
config is optional. Disable a third-party plugin without uninstalling:
`[plugins.<name>] enabled = false`.

The plugin set is fixed at agent process startup — config edits don't
take effect until next session.

## Plugin introspection (for self-improvement)

The "agent writes its own plugin" workflow needs a way for the agent
to inspect what's loaded:

- **`list_plugins()`** — agent-callable tool. Returns name, version,
  source tier, enabled state, and `[provides]` for each loaded
  plugin.

That's the v1 surface. The agent can write
`<config-dir>/plugins/<name>/{plugin.py, manifest.toml}`, ask the
user to restart, and check `list_plugins()` after the restart to
confirm. Hot reload is a v2 feature.

## Tool-name collisions and graceful degradation

Sessions persist tool calls in conversation history. A plugin removed
or replaced between sessions means the LLM may try to call a tool
that no longer exists. The graceful behavior:

- **At plugin load:** if two plugins try to register the same tool
  name, **soft-fail**. The first plugin to register wins (load order
  is alphabetical by plugin name within tier — deterministic). The
  second plugin gets a `warn` log and skips that registration. The
  agent starts; the conflicting plugin still loads with whatever
  registrations did succeed. **Failing the whole agent on a single
  tool-name conflict is too brittle for an ecosystem.**
- **At LLM call time:** if the LLM calls an unregistered tool,
  pyagent returns a deterministic error instead of `KeyError`:

  ```
  <tool 'recall_memory' is not currently available.
  Available tools: read_ledger, write_ledger, read_file, ...
  This tool was provided by plugin 'memory-vector' (currently disabled
  or removed). To restore: enable the plugin in config.toml.>
  ```

  The "was provided by" suggestion uses `[provides]` from manifests
  of installed-but-disabled plugins. The LLM sees the catalog and
  adapts on the next turn.
- **Historical tool calls in transcripts are facts.** They don't get
  re-run on resume. The LLM can read what happened without the tool
  needing to exist now.

## Error handling

| Failure | Effect |
| --- | --- |
| Manifest malformed | Plugin skipped, warn logged. Agent starts. |
| `api_version` mismatch | Plugin skipped, warn logged. Agent starts. |
| `requires.*` not met | Plugin skipped, info logged. Agent starts. |
| `[provides]` ↔ `register()` mismatch | Plugin fails to load, warn logged. Agent starts without it. |
| ImportError on plugin module | Plugin skipped, warn logged. Agent starts. |
| `register(api)` raises | Partial registrations rolled back. Warn logged. Agent starts. |
| Tool name conflict (two plugins) | First plugin wins; second's registration skipped with a warn. Agent starts. |
| Hook callback raises | Caught and logged with plugin name. Agent loop continues. |
| Plugin tool raises | Caught by `Agent._route_tool` like any other tool error; LLM sees the error message. Agent loop continues. |

## Versioning

`api_version` is the pyagent ↔ plugin contract. A single integer-as-
string ("1"). Pyagent supports exactly one value; plugins declaring a
different value are skipped.

A plugin's *own* on-disk data format is its concern. A plugin that
persists state should write a `version` file in its data dir on
first write and validate it on `on_session_start`. On mismatch, the
plugin chooses: migrate, warn, or refuse. Pyagent does not enforce.

## What plugins MUST NOT do

- Import from `pyagent.agent`, `pyagent.agent_proc`, `pyagent.session`,
  `pyagent.subagent`, `pyagent.tools` — unstable.
- Mutate `agent.conversation`, `agent.tools`, or any other internal
  reachable through closures. The API exposes what's supported.
- Spawn threads as cheap concurrency. Pyagent uses process-based
  isolation; plugins follow the same rule. Asyncio inside a tool body
  is allowed.
- Block the agent loop on slow synchronous work. If you need to call
  an embedding API or write to a vector index, queue it as a
  fire-and-forget background task and let the next turn's renderer
  pick up the result.
- Block on network in `register(api)`. That delays agent startup.
- Print to stdout/stderr directly. Use `api.log(...)`.
- Write outside `api.user_data_dir` without going through
  `permissions.require_access`.
- Communicate with pyagent core through filesystem side channels
  (sentinel files, shared paths the CLI is expected to poll). If a
  capability is missing, the API needs to grow — open an issue.

## Plugin runtime vision (v2 north star)

v1 ships plugins as **synchronous observer extensions** to the main
agent's turn. The eventual model is bigger: plugins as **autonomous
actors** that can be triggered by external events, run their own LLM
turns in isolated agents, and communicate back into user-facing
sessions.

The four communication shapes a plugin can use to surface results:

| Shape | Direction | Sync | Use case | v1 status |
| --- | --- | --- | --- | --- |
| **1. Side-effect + log** | plugin → terminal | sync | Plugin did a thing | shipped (`api.log`) |
| **2. One-way notification** | plugin → terminal *or* session, async | async | "GitHub issue arrived" | v2 (`api.deliver`) |
| **3. Interactive query** | plugin ↔ human, blocking | sync | "Prompt has a secret; proceed?" | v2 (`api.ask_user`) |
| **4. Hook return-value control** | controlling hook → pyagent | sync | Reject/modify a turn before LLM call | v2 (controlling hooks) |

### v2 API sketches

These are not implemented. Documented so v1's shape doesn't
contradict them.

```python
api.deliver(text: str, *, kind: str = "info") -> None
"""Shape 2. Implicit recipient: the session this plugin's process is
hosting. kind="info" → terminal-only; kind="user_message" → appended
to the session as a user-role turn."""

api.ask_user(question, choices=None, timeout=None) -> str | None
"""Shape 3. Same machinery as the existing permission-prompt flow."""

agent = api.create_agent(*, model=None, tools=None, system_prompt="")
"""Spin up an isolated agent with its own conversation, tools, and
model. Reuses pyagent's existing Agent / subagent.py machinery. Plugin
uses this for LLM-driven work without polluting the user's session."""

@api.on_external_event("github.issue.opened")
def handler(event: dict) -> None: ...
"""External-event hook category. Plugin gets triggered by something
other than the main agent's turn."""

@api.before_user_prompt
def safety_check(prompt: str) -> tuple[str, str | None]: ...
"""Controlling hook (shape 4). Return value: ('pass', None),
('modify', new_prompt), or ('reject', reason)."""

@api.on_compact
def summarize(messages) -> list[Message]: ...
"""Conversation compaction at breakpoints. Returns rewritten history."""
```

### Canonical v2 example

```python
def register(api):
    @api.on_external_event("github.issue.opened")
    def handle_issue(event):
        agent = api.create_agent(
            model=api.plugin_config.get("model", "anthropic/claude-haiku-4-5-20251001"),
            tools=["fetch_url"],
            system_prompt="Summarize GitHub issues concisely.",
        )
        summary = agent.run(f"Summarize: {event['issue_url']}")
        api.deliver(
            f"New issue #{event['number']}: {summary}",
            kind="user_message",
        )
```

### What v1 does to leave room for this

- API is additive-friendly: `register(api)` receives one object;
  v2 methods on `api` don't break v1 plugins.
- `api_version` is the contract knob; v2 capabilities live behind a
  bumped version.
- v1 hooks framed as **observers** so adding **controlling hooks** in
  v2 doesn't contradict the v1 framing.
- Plugin lifecycle = pyagent process lifecycle. A future daemon mode
  may extend this.

## Other v2 / future items

- **Plugin slash commands.** `/memory clear`, `/memory show`.
- **Hot reload.** Edit a plugin file, pick up next turn without
  restart.
- **Subprocess sandbox.** `[load] sandbox = true` for untrusted
  plugins.
- **Plugin ↔ plugin communication.** Layered memory composing.
- **Capability enforcement.** Real gating on filesystem/network.
- **Eval framework.** Replayable conversation fixtures + per-plugin
  metric surface.
- **Async renderers.** Currently sync; vector recall is one turn
  stale as a result.
- **Conversation rewriting.** `on_compact`, with rules about what
  saved transcripts look like.
- **Plugin runtime health.** `plugin_health(name)` agent tool
  returning recent error/timeout counts so the agent can iterate on
  its own plugins.
- **Per-plugin reset.** `pyagent-plugins reset <name>`.
- **Multi-session.** A pyagent process hosting multiple user-facing
  sessions (Telegram bridge with N chats). Adds `session=` to
  `api.deliver` and friends.
- **Resume notice on tool-name change.** A one-time synthetic notice
  at session resume when the tool catalog has shifted since last
  turn ("memory-markdown was replaced by memory-vector; tools renamed:
  read_ledger→recall_memory"). Today's missing-tool error is reactive;
  a proactive notice would save a wasted turn.
