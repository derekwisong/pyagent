---
name: write-plugin
description: Author a new pyagent plugin — manifest schema, PluginAPI surface, hooks, drop-in layout, activation. Load this when the user asks you to create or modify a plugin.
---

# Writing a pyagent plugin

A plugin is active code that extends pyagent at runtime. Unlike a
skill (which is passive markdown the agent loads on demand), a plugin
runs *in the agent process* and can:

- **Register tools** the LLM can call.
- **Contribute prompt sections** that re-render before every LLM call.
- **Observe the conversation loop** — assistant turns, tool calls.
- **React to lifecycle events** (`on_session_start`, `on_session_end`).

## Plugin vs skill vs tool

- A **tool** is a Python function the LLM can call directly.
- A **skill** is markdown instructions the LLM loads on demand. No
  code, just text. Good for "how to use feature X."
- A **plugin** is Python code that *registers* tools and prompt
  sections. Good for "I want to add a tool that does Y" or "I want
  the LLM to always see Z in its prompt."

If the user wants the LLM to learn a *workflow* — write a skill. If
they want to add *capability* (a new tool, a new prompt-injected
context block, a new memory backend) — write a plugin.

## Directory layout

A drop-in plugin is a directory:

    <root>/<plugin-dir-name>/
        manifest.toml         required — metadata, validated without executing code
        plugin.py             required — Python entrypoint with def register(api)
        defaults/             optional — bundled data files
        helper.py             optional — sibling modules; import via `from . import helper`

The **directory name** controls load order (sorted alphabetically
within tier; prefix with `01-`, `02-`, etc. to influence). The
**manifest's `name` field** is the plugin's identity — used by config,
conflict detection, and the rich missing-tool error. They can differ.

No `__init__.py` is needed at the top level for drop-ins. Pyagent's
loader treats `plugin.py` as the entrypoint via a synthetic spec.
Subdirectories you treat as Python subpackages (`embeddings/`, etc.)
do need their own `__init__.py` per standard Python rules.

## Where to put `<root>`

Three tiers, later wins on name collision:

- `<package>/plugins/` — **bundled** with pyagent. Read-only from the
  agent's perspective; you don't author here. Gated by
  `built_in_plugins_enabled` in `<config-dir>/config.toml`.
- `<config-dir>/plugins/` — **user-wide**. Available across every
  project the user runs pyagent in.
- `./.pyagent/plugins/` — **project-local**. Overrides user-wide and
  bundled plugins of the same name.

Default to `<config-dir>/plugins/` (user-wide) unless the user says
otherwise. Confirm scope once if unclear; don't guess.

## manifest.toml

```toml
name = "my-plugin"               # globally unique; the plugin's identity
version = "0.1.0"                # the plugin's own version
description = "What the plugin does, in one sentence."
api_version = "1"                # the pyagent plugin API version

# What the plugin promises to register. Validated at load time:
# pyagent fails the plugin loud if register() registers anything not
# listed, or fails to register everything listed. Also powers the
# rich missing-tool error.
[provides]
tools = ["recall_memory", "save_fact"]
prompt_sections = ["recalled-memories"]

# Optional eligibility — plugin is skipped (logged) if any fail.
[requires]
python = ">=3.11"
env = []                         # required env vars (e.g. ["OPENAI_API_KEY"])
binaries = []                    # required CLI binaries on PATH

# Spawn-tree behavior. Default true: plugin loads in every agent
# process, including subagents. Set false for plugins that aren't
# parallel-safe — they only load in the root agent.
[load]
in_subagents = true
```

**Required fields:** `name`, `version`, `description`, `api_version`.
The `[provides]` table is mandatory if you register anything.

## plugin.py — the register entrypoint

```python
def register(api):
    # Tools, prompt sections, and hooks all register here.
    # Must return synchronously; nothing slow, no network calls.
    ...
```

`api` is a `PluginAPI` instance — the **only** seam between plugin
code and pyagent internals. Don't import from `pyagent.agent`,
`pyagent.agent_proc`, `pyagent.session`, `pyagent.subagent`, or
`pyagent.tools` — those are unstable. Importing `pyagent.paths` for
config-dir resolution is allowed.

### Read-only attributes

```python
api.config_dir        # Path: <config-dir>
api.workspace         # Path: cwd at agent startup
api.user_data_dir     # Path: <data-dir>/plugins/<name>/  — lazy-created
api.plugin_config     # dict: this plugin's [plugins.<name>] table
api.plugin_name       # str: this plugin's name
```

`user_data_dir` is where you persist plugin state. Don't invent
paths — use this one.

### Registration methods

```python
api.register_tool(name: str, fn: Callable) -> None
"""Add an LLM tool. The function's signature, type hints, and
docstring become the schema sent to the model. First Args: line
in the docstring becomes the parameter description."""

api.register_prompt_section(
    name: str,
    renderer: Callable[[PromptContext], str],
    *,
    volatile: bool = False,
) -> None
"""Inject markdown into the system prompt. Set volatile=True for
content that changes turn-to-turn (vector recall, recently-touched
files) so it lives AFTER the cache_control marker and doesn't
invalidate the cached prefix."""
```

### Lifecycle hooks

```python
api.on_session_start(fn: Callable[[Session], None])
"""Fired once after the agent is ready. Seed defaults, validate
config, warm caches."""

api.on_session_end(fn: Callable[[Session], None])
"""Fired on clean shutdown. Best-effort — won't run on SIGKILL.
Don't put durability-critical work here; persist incrementally."""
```

### Observation hooks

All v1 hooks are **observers** — return values are ignored. They can
read but not modify or reject what they observe.

```python
api.after_assistant_response(fn: Callable[[str], None])
"""Fired once after each LLM turn that produced text, with the
concatenated text. A turn with only tool calls and no text doesn't
fire it. Use for fact extraction, vector indexing, transcript
persistence."""

api.before_tool_call(fn: Callable[[str, dict], None])
"""Fired before each tool call. Use for logging, audit, prefetch."""

api.after_tool_call(fn: Callable[[str, dict, str], None])
"""Fired after each tool call with (name, args, result). Use for
extracting entities, indexing tool output, episodic memory keyed
on tool patterns."""
```

### Utility

```python
api.log(level: str, message: str)
"""Emit a structured log line tagged with the plugin name. Levels:
'debug', 'info', 'warn', 'error'. Goes through the same event
stream as info events from the agent — the human sees plugin
output."""
```

## PromptContext and Message

`register_prompt_section` renderers receive a `PromptContext`:

```python
ctx.recent_messages   # tuple[Message, ...] — last 8 conversation turns
```

Each `Message` is a frozen dataclass with `role` ("user" |
"assistant") and `text`. Tool-result turns have empty text;
assistant turns with only tool calls have empty text. Plugins read
`ctx.recent_messages[-1].text` to know what the user just asked.

For static prompt sections (instructional prose, fixed guidance),
ignore `ctx`. For dynamic content (vector recall, "currently relevant
memories"), read `ctx.recent_messages` and use `volatile=True`.

## Hook timing

Plugins should not block the agent loop. Hooks run synchronously and
to completion — there are no enforced timeouts. A plugin that hangs
in a hook hangs the agent.

If you need slow work (embedding API calls, network requests),
**fire-and-forget**: queue the work as a background side effect,
persist when it completes, and let the next turn's renderer pick up
the persisted result. This means recall-driven memory is **always
one turn stale** — you index turn N's response and the renderer for
turn N+1 reads it. That's the only shape that fits Python's
no-preemption reality.

## Activation lifecycle

1. New plugins are picked up automatically by pyagent's run-loop
   rescan: when a plugin directory appears on disk mid-session, the
   loader registers its tools at the top of the next turn and the
   agent receives a `[plugin plugin-loader notes]: loaded <name>
   v<ver>; tools=[…]` system message confirming what landed. No
   restart needed for new plugins.
2. *Editing* an already-loaded plugin still requires a restart —
   the synthetic module name is cached in `sys.modules`. Hot-reload
   for in-place edits is intentionally out of scope (creation is
   the rare event reload was built for).
3. After authoring, run `pyagent-plugins list` to confirm the
   plugin was discovered and what tier it's in.

## What plugins MUST NOT do

- Import from `pyagent.agent`, `pyagent.agent_proc`,
  `pyagent.session`, `pyagent.subagent`, or `pyagent.tools`.
- Mutate `agent.conversation`, `agent.tools`, or any other internal.
- Spawn threads as cheap concurrency (use process-based isolation or
  asyncio inside tool bodies).
- Block on network in `register(api)`. Network calls belong inside
  tools or hooks.
- Print to stdout/stderr directly (use `api.log`).
- Communicate with pyagent core via filesystem side channels (sentinel
  files, shared paths the CLI is expected to poll). If a capability is
  missing, file an issue.

## Recipe: writing a plugin end-to-end

When the user asks for a new plugin:

1. **Pick a name and scope.** Confirm: user-wide
   (`<config-dir>/plugins/`) or project-local (`./.pyagent/plugins/`).
   Default user-wide unless they say otherwise.
2. **Decide the surface.** What tools? What prompt sections? Which
   hooks? Sketch the manifest's `[provides]` block first — it forces
   you to commit to the surface before writing code.
3. **Make the directory** with `manifest.toml` and `plugin.py`. Add
   helper modules and `defaults/` data files if needed.
4. **Write `manifest.toml`** with the required fields and the
   declared `[provides]` you sketched.
5. **Write `plugin.py`**: a single `register(api)` that calls
   `api.register_tool(...)`, `api.register_prompt_section(...)`,
   `api.on_session_start(...)`, etc. Match what `[provides]` declared
   exactly — pyagent fails plugins loud on mismatch.
6. **The plugin auto-loads on the next turn.** The loader's
   run-loop rescan picks up new plugin directories and you'll get a
   `[plugin plugin-loader notes]: loaded <name> ...` message
   confirming the tools landed. No restart, no `list_plugins`
   call — the system message is the confirmation.

If the user wants to ship it as a bundled plugin: copy the directory
under `pyagent/plugins/` in the source tree and add the name to
`built_in_plugins_enabled` in their `config.toml`. That's a
developer-side workflow, not something to do from the agent.

## Reference

For deeper context — design rationale, the v2 runtime vision
(`api.create_agent`, `api.deliver`, controlling hooks, external
triggers), and competitive landscape — read `docs/plugin-design.md`
in the pyagent repo. The bundled `pyagent/plugins/memory/` plugin
is a complete worked example exercising every v1 element.
