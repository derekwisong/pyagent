# Pyagent Plugins — Feature Summary

What plugins are, what they let users do, and the public API surface
in one page. Companion to `plugin-design.md` (the deep dive).

## What is a plugin?

A plugin is a Python module that extends pyagent at runtime. It can:

- **Add tools** the LLM can call.
- **Contribute prompt sections** that re-render before every LLM call.
- **Observe the conversation loop** — assistant turns, tool calls.
- **React to lifecycle events** — session start and end.

A plugin is *not* a skill. Skills are passive markdown the agent
loads on demand; plugins are active code that runs alongside the
agent.

## Why plugins

- **Replace built-in subsystems.** Pyagent's memory system is itself
  a plugin (`memory-markdown`). Want a vector-backed memory? Install
  `pyagent-memory-vector`, the agent gets new tools.
- **Layer subsystems.** Multiple memory plugins coexist as long as
  their tool names don't collide — markdown ledgers + vector recall +
  episodic memory, simultaneously.
- **Bundle related tools.** A `git-tools` plugin can register a
  half-dozen git-aware tools without each landing in core.
- **Local hacks without forking.** Drop a `plugin.py` into
  `~/.config/pyagent/plugins/myhack/`, restart, it's live.
- **Enable agent self-improvement.** The agent can author a plugin
  via `write_file`, ask the user to restart, and check the result via
  the `list_plugins()` tool.

## Three ways to install a plugin

1. **Bundled** — ships with pyagent. Toggle in `config.toml`:
   ```toml
   built_in_plugins_enabled = ["memory-markdown"]
   ```
2. **Pip-installed** — third-party package declares an entry point in
   its `pyproject.toml`; `pip install pyagent-memory-vector` surfaces
   it next session.
3. **Drop-in directory** — author a folder with `manifest.toml` +
   `plugin.py` at:
   - `~/.config/pyagent/plugins/<name>/` (per-user)
   - `./.pyagent/plugins/<name>/` (per-project)

Project beats user beats bundled, by name. `pyagent-plugins list`
flags shadowing — a stale drop-in masking a fresh `pip install` is
visible, not silent.

## Configure a plugin

Per-plugin config in `config.toml`:

```toml
[plugins.memory-vector]
backend = "lancedb"
embedding_model = "text-embedding-3-small"
```

Read by the plugin via `api.plugin_config`. Disable without
uninstalling:

```toml
[plugins.memory-vector]
enabled = false
```

The plugin set is fixed at agent process startup — config edits take
effect on the next session.

## Listing what's loaded

```
pyagent-plugins list
```

One line per discovered plugin: name, version, source tier, enabled
state, declared `[provides]`, shadowing warnings.

## Writing a plugin (the short version)

```python
# plugin.py
def register(api):
    def hello(name: str) -> str:
        """Say hi.

        Args:
            name: who to greet.
        """
        return f"hi, {name}"

    api.register_tool("hello", hello)
```

```toml
# manifest.toml
name = "hello"
version = "0.1.0"
description = "Trivial example."
api_version = "1"

[provides]
tools = ["hello"]
```

Drop both files into `~/.config/pyagent/plugins/hello/`. Restart. The
LLM has a `hello` tool. Two files, no boilerplate. See
`docs/examples/memory_markdown/` for a complete plugin exercising the
full surface.

### Multi-file plugins and data files

A plugin's directory is a Python package. `plugin.py` is the
entrypoint; helper modules and data files live alongside and are
imported with relative imports:

```
my-plugin/
    manifest.toml
    plugin.py              # def register(api): ...
    extraction.py          # from . import extraction
    embeddings/            # from .embeddings import client
        __init__.py
        client.py
    defaults/
        PROMPT.md          # Path(__file__).parent / "defaults" / "PROMPT.md"
```

### Controlling load order with directory prefixes

Within each tier, plugins load in sorted directory-name order. The
manifest's `name` field is the plugin's identity; the directory name
is just disk layout. Prefix with numeric ordinals to control order:

```
~/.config/pyagent/plugins/
    01-memory-vector/      manifest: name = "memory-vector"
    02-memory-markdown/    manifest: name = "memory-markdown"
```

Same pattern as `init.d` / `conf.d`. Combined with the soft-fail
tool-conflict rule (first-registered wins), this gives a deterministic
way to resolve conflicts without editing manifests.

## Public API surface (v1)

The `api` object passed to `register` is the only seam. **13 elements
total.**

**Read-only attributes:**

| Attribute | What |
| --- | --- |
| `api.config_dir` | `<config-dir>` (Path) |
| `api.workspace` | cwd at agent startup |
| `api.user_data_dir` | `<config-dir>/plugins/<name>/`, lazy-created |
| `api.plugin_config` | this plugin's `[plugins.<name>]` table |
| `api.plugin_name` | this plugin's name |

**Registration (called inside `register(api)`):**

| Call | What it does |
| --- | --- |
| `api.register_tool(name, fn)` | Add an LLM tool. |
| `api.register_prompt_section(name, renderer, *, volatile=False)` | Inject markdown into the system prompt. `name` is a unique identifier matching `[provides] prompt_sections` in the manifest. `volatile=True` keeps prompt-cache hits when the section's content changes turn-to-turn. |

**Lifecycle hooks:**

| Hook | Fired when |
| --- | --- |
| `on_session_start(fn)` | After agent ready, before first turn. Agent waits for all to return before accepting prompts. |
| `on_session_end(fn)` | Clean shutdown (won't run on SIGKILL). |

**Observation hooks** (return values ignored — observers only):

| Hook | Receives |
| --- | --- |
| `after_assistant_response(fn)` | text |
| `before_tool_call(fn)` | name, args |
| `after_tool_call(fn)` | name, args, result |

**Utility:**

| Call | What |
| --- | --- |
| `api.log(level, message)` | Structured log line, tagged with plugin name |

The renderer receives a `PromptContext` with one field:
`recent_messages` (read-only view of the last 8 conversation turns).
Vector recall and dynamic prompt sections read this; static sections
(like `memory-markdown`'s instructional prose) ignore it.

## Hook timing

Plugins should not block the agent loop. Pyagent calls hooks
synchronously and runs them to completion — no enforced timeouts. A
plugin that needs to do slow work (embedding, network calls) should
queue it as a background task and let the next turn's renderer pick
up the result. **A plugin that hangs in a hook hangs the agent**;
same blast radius as a hung tool today.

This means recall-driven memory plugins are always one turn stale —
the plugin indexes the assistant's text in `after_assistant_response` and
the next turn's renderer reads the persisted result. That's the only
shape that fits Python's no-preemption reality.

## Spawn-tree behavior

Pyagent runs the agent in a subprocess; subagents are also
subprocesses. **Plugins reload per process** — each gets its own
plugin instances. No shared in-memory state across the tree.

If your plugin persists state, you must coordinate concurrent access
yourself (file locks, sqlite WAL). Pyagent does not provide a lock
primitive — use whatever your backend supports.

A plugin that isn't parallel-safe can opt out of subagent loading:

```toml
[load]
in_subagents = false
```

Root agent loads it; subagents skip it.

## Tool-name collisions and missing tools

- **At plugin load:** if two plugins try to register the same tool
  name, the first plugin (alphabetical by plugin name) wins; the
  second's registration is skipped with a warning. The agent starts.
- **At LLM call time:** if the LLM tries to call a tool that isn't
  registered, pyagent returns a deterministic error citing the
  current catalog and (when applicable) the disabled plugin that used
  to provide it. The LLM adapts on the next turn.

This makes long-running sessions safe — Telegram or Discord bridges
that keep a session open for months see plugins come and go without
breaking the conversation.

## What plugins can't do (v1)

- Import from internal modules. Use `api.*` only.
- Mutate the conversation directly. Use the observation hooks
  (`after_assistant_response`, `after_tool_call`) — return values are
  ignored.
- Reject or modify a turn before the LLM call (controlling hooks are
  v2).
- Make their own LLM calls or spawn isolated agents (v2:
  `api.create_agent`).
- Spawn threads for background concurrency.
- Add slash commands or LLM providers.
- Survive a plugin-code change without restart. Hot reload is v2.

## Bundled out of the box

| Plugin | Default enabled? |
| --- | --- |
| `memory-markdown` | yes |

`memory-markdown` is the existing `read_ledger`/`write_ledger` system
ported to the plugin API. Disabling it removes the ledger tools and
the SOUL-level memory prose entirely — clean replacement surface for
alternative memory backends. See `plugin-memory-migration.md` for the
staged cutover.
