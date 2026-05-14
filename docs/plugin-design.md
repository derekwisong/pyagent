# Pyagent Plugin System

A plugin is a Python module that extends pyagent at runtime — registering
tools, contributing prompt sections, observing or controlling the
conversation loop, and (optionally) registering LLM providers. The
single seam between plugin code and pyagent internals is `PluginAPI`
(`pyagent/plugins/__init__.py`). A working plugin can fit in ~80 lines.

## Concepts

| Concept | Lives where | Role |
| --- | --- | --- |
| **Tool** | `pyagent/tools.py`, plugin code | Python function the LLM can call. |
| **Skill** | `<config-dir>/skills/<name>/SKILL.md` | Markdown the agent loads on demand. Passive. |
| **Plugin** | `<config-dir>/plugins/<name>/`, entry-point package, or `pyagent/plugins/<name>/` | Active code that registers tools/providers, contributes prompt text, observes/controls the loop. |
| **Role** | `[models.<name>]` in `config.toml` | Named subagent preset (model + tool allowlist + prompt). |
| **Provider** | `pyagent/llms/*.py` or plugin | LLM client. |

**Vocabulary:** *Session* — the `Session` in `pyagent/session.py`; one
pyagent invocation hosts exactly one (storage at
`.pyagent/sessions/<id>/`). *Terminal* — rendered output the human
reads; `api.log(...)` writes there. *Observer hook* — return value
ignored. *Controlling hook* — return value can block/mutate/inject.
A plugin's `api_version` selects which contract its `before_tool_call`
and `after_tool_call` hooks use (see Hook contracts below).

## The plugin contract

A plugin module exposes `def register(api: PluginAPI) -> None`.
Plugins **must not** import from `pyagent.agent`, `pyagent.agent_proc`,
`pyagent.session`, `pyagent.subagent`, or `pyagent.tools` — these are
unstable. `pyagent.paths` is fine. Alongside the module, a
`manifest.toml` is metadata that pyagent reads and validates without
executing plugin code, so a malformed manifest never crashes the
loader.

### Manifest schema

```toml
name = "memory"                    # globally unique
version = "0.2.0"
description = "Markdown-file memory backend."
api_version = "1"                  # "1" or "2"

# Validated at load: pyagent fails the plugin loud if register()
# registers anything not listed, or fails to register everything
# listed. Powers the rich missing-tool error.
[provides]
tools = ["create_memory", "read_memory", "recall_memory"]
prompt_sections = ["memory-guidance"]
providers = []                     # optional

# Optional eligibility — plugin is skipped (logged) if any fail.
[requires]
python = ">=3.11"
env = []                           # required env vars
binaries = []                      # required CLI binaries on PATH

# Default true: plugin loads in every agent process. Set false for
# plugins that aren't parallel-safe.
[load]
in_subagents = true
```

## The PluginAPI surface

Verified against `pyagent/plugins/__init__.py:PluginAPI`. Plugins call
methods only from inside `register(api)`; the API freezes on return.

**Read-only attributes:** `config_dir`, `workspace`, `user_data_dir`
(lazy-created `<data-dir>/plugins/<name>/`), `plugin_config` (this
plugin's `[plugins.<name>]` table), `plugin_name`.

**Registration:**

- `register_tool(name, fn, *, role_only=False)` — register an LLM tool.
  `role_only=True` keeps the tool out of the root agent's default set;
  only agents whose role allowlist names it explicitly get it (e.g.
  `delete_memory` exposed only to a curator role).
- `register_prompt_section(name, renderer, *, volatile=False)` — a
  function returning markdown injected into the system prompt.
  `volatile=True` places it after the last `cache_control` marker so
  its content can change turn-to-turn without invalidating the cached
  span. `volatile=False` renderers must be pure functions of
  `PromptContext`.
- `register_provider(name, factory, *, default_model="", env_vars=(), list_models=None)`
  — register an LLM provider exposed as `<name>/<model>` for `--model`.
  Conflicts with built-in providers raise at load.

**Lifecycle hooks:**

- `on_session_start(fn)` — `fn(session)`. Fires sequentially after the
  agent signals "ready". The agent does not dequeue user prompts until
  all `on_session_start` callbacks return.
- `on_session_end(fn)` — `fn(session)`. Best-effort on clean shutdown;
  won't run on SIGKILL. Don't put durability-critical work here.

**Observation / controlling hooks:**

- `after_assistant_response(fn)` — `fn(text)`. Fires once per LLM turn
  that produced text. Observer only.
- `before_tool_call(fn)` and `after_tool_call(fn)` — signatures and
  return semantics depend on the plugin's `api_version` declaration.
  See **Hook contracts** below.

**Utilities:**

- `write_session_attachment(tool_name, content, suffix="")` — write to
  the session's attachments dir. Returns `None` if no session is active
  (bench harness). Most plugins prefer returning an `Attachment` from a
  tool instead — the render path writes the file and glues inline
  rendering with the `[also saved: <path>]` footer.
- `call_tool(name, **kwargs)` — invoke another registered tool from
  inside a tool body. Returns the raw string output (with `<… error: …>`
  markers propagated; never raises). Resolves through the agent's
  effective registry, so subagent role allowlists apply. Bounded by
  `CALL_TOOL_DEPTH_CAP=4`. NOT exposed to the LLM.
- `log(level, message)` — structured log line tagged with plugin name.
  Levels: `debug`, `info`, `warn`, `error`.

**PromptContext:** passed to renderers — `recent_messages: tuple[Message, ...]`
where each `Message` is `(role: str, text: str)`. Window is the last 8
turns. A recall plugin reads `recent_messages[-1].text` to know what
the user just asked.

## Hook timing and enforcement

Hooks run on the main thread, in order, synchronously. A hook that
hangs hangs the agent; a hook that raises is caught and logged and the
loop continues. Pyagent imposes no deadlines on hook bodies — Python
can't preempt running code without leaking threads or breaking
C-extension calls.

For slow work (embedding, network), fire-and-forget into a background
task and let the next turn's renderer pick up the persisted result.
Canonical recall shape (always one turn stale — the only shape that
fits):

```
after_assistant_response:   embed + index, persist
register_prompt_section (volatile):  read pre-computed index,
                                     retrieve, return markdown
```

## Hook contracts

`before_tool_call` and `after_tool_call` have two contracts; the
plugin's `api_version` selects which one is honored. Pyagent runs
both contracts side by side in the same process.

**`api_version = "1"` — observer only:**

- `before_tool_call(fn)` — `fn(name, args)`. Return value ignored.
- `after_tool_call(fn)` — `fn(name, args, result)`. Return value
  ignored.

**`api_version = "2"` — controlling:** plugins may return result
dataclasses to block/mutate/inject.

`before_tool_call(fn)` — `fn(name, args)`, return optional
`ToolHookResult`:

```python
@dataclass(frozen=True)
class ToolHookResult:
    decision: Literal["allow", "block", "mutate"] = "allow"
    reason: str = ""                  # required when decision="block"
    mutated_args: dict | None = None  # required when decision="mutate"
    extra_user_message: str = ""
```

- `block` — tool not executed; model sees `<blocked by plugin <name>:
  <reason>>`. INFO log emitted. Short-circuits later `before_tool_call`
  hooks on this call.
- `mutate` — tool runs with `mutated_args`; chains across plugins
  (each later plugin sees the earlier's args). Mutated dict persists
  into conversation history.
- `extra_user_message` — prepended to next assistant turn as a
  user-role message tagged `[plugin <name> notes]: <text>`, via the
  same `pending_async_replies` channel async-subagent notes use.
  Accumulates across hooks that ran (including the one that blocked).

`after_tool_call(fn)` — `fn(name, args, result, is_error)`, return
optional `AfterToolHookResult`:

```python
@dataclass(frozen=True)
class AfterToolHookResult:
    extra_user_message: str = ""
    replace_result: str | None = None
```

`is_error` is the harness-computed failure signal
(`pyagent.tools.is_error_result`); plugins don't sniff result strings.
`replace_result` overrides the tool-result string the model sees;
chains across plugins, last-wins. Non-string replacements are dropped
with a warning.

Hooks fire **before** permission checks: a controller's `block` can
short-circuit before the human sees a permission prompt.

**Worked example:** `pyagent/plugins/strategic_reevaluation/__init__.py`
is an `api_version = "2"` hook plugin (no tools, no prompt sections)
that tracks consecutive `edit_file` failures per path and injects an
`extra_user_message` after three failures on the same path.

## Discovery

Three tiers, in load order. **Later tiers win** on name collision:

1. **Bundled** — `pyagent/plugins/<name>/`. Filtered against
   `built_in_plugins_enabled` in `config.toml`.
2. **Entry points** — packages declaring
   `[project.entry-points."pyagent.plugins"]`. Discovered via
   `importlib.metadata`.
3. **Drop-ins** — `<config-dir>/plugins/<name>/` and
   `./.pyagent/plugins/<name>/`. Project beats user beats bundled.

Within a tier, plugins load in sorted directory-name order. The
manifest's `name` is identity (config, conflicts); the directory name
is layout. They can differ — prefix `01-`, `02-`, … to influence load
order without renaming the plugin. `pyagent-plugins list` shows tier
and flags shadowing.

## Plugin packaging

A plugin's directory is a Python package. `plugin.py` is the entrypoint
that exports `register(api)`; helpers and data files live alongside.

```
my-plugin/
    manifest.toml
    plugin.py              # def register(api): ...
    extraction.py
    embeddings/
        __init__.py
    defaults/
        PROMPT.md
```

```python
from . import extraction
from pathlib import Path
TEMPLATE = (Path(__file__).parent / "defaults" / "PROMPT.md").read_text()
```

The drop-in loader uses `importlib.util.spec_from_file_location` with
`submodule_search_locations=[plugin_dir]`, so `from . import …`
resolves to siblings of `plugin.py` with no top-level `__init__.py`.
Entry-point and bundled plugins are real Python packages — they do
need a top-level `__init__.py`. Subdirectories used as subpackages
always need `__init__.py` (standard Python rule).

## Configuration

```toml
built_in_plugins_enabled = ["memory"]   # replaces the bundled default list

[plugins.memory-vector]
backend = "lancedb"
enabled = false   # explicit disable without uninstalling
```

Plugins absent from `[plugins.<name>]` get `plugin_config = {}`.

The plugin set is fixed at startup, except for
`LoadedPlugins.rescan_for_new`, which runs at the top of the agent's
main loop so a plugin the LLM just authored is callable on its next
API turn. Rescan is add-new only; in-place edits to a loaded plugin's
source require a process restart.

## Tool-name collisions and graceful degradation

Sessions persist tool calls. A plugin removed or replaced between
sessions means the LLM may try to call a tool that no longer exists.

- **At load:** name conflict soft-fails — first registration wins,
  duplicate is skipped with a warn. The agent starts; the conflicting
  plugin still loads with whatever registrations succeeded.
- **At LLM call time:** an unregistered tool returns a deterministic
  error citing the plugin from manifest `[provides]` when known
  (`format_missing_tool_error` in `pyagent/plugins/__init__.py`).
- **Historical tool calls in transcripts are facts** — not re-run on
  resume; the LLM reads what happened without the tool existing now.

## Error handling

A bad plugin should fail loud and stay out of the way. The agent
always starts. Failure modes:

- Malformed manifest, unsupported `api_version`, unmet `requires.*`,
  ImportError, `register()` raises, `[provides]` ↔ registration
  mismatch — **plugin skipped**, warn (or info) logged.
- Tool/section/provider name conflict — **first wins**, duplicate
  skipped with warn.
- Hook callback raises — caught and logged; agent loop continues.
- Plugin tool raises — caught by `Agent._route_tool` like any tool;
  LLM sees the error marker.

## Versioning

`api_version` is the pyagent ↔ plugin contract. The supported set is
`SUPPORTED_API_VERSIONS = {"1", "2"}`; plugins declaring any other
value are skipped at load time. Plugins at different `api_version`
values coexist in the same process.

A plugin's *own* on-disk data format is its concern. A plugin that
persists state should write a `version` file in its data dir on first
write and validate it on `on_session_start`. Pyagent does not enforce.

## Spawn-tree behavior

Plugins re-bootstrap independently per agent process — root and each
subagent load their own instances. No shared in-memory state across
the spawn tree; on-disk coordination is the plugin's job (file locks,
sqlite WAL, etc.). `[load] in_subagents = false` opts out — only the
root loads. No plugin object survives `multiprocessing.spawn`.

## What plugins MUST NOT do

- Import from `pyagent.agent`, `pyagent.agent_proc`, `pyagent.session`,
  `pyagent.subagent`, `pyagent.tools` — unstable.
- Mutate `agent.conversation`, `agent.tools`, or any other internal
  reachable through closures.
- Spawn threads as cheap concurrency. Asyncio inside a tool body is
  fine.
- Block the agent loop on slow synchronous work — fire-and-forget into
  a background task and pick results up next turn.
- Block on network in `register(api)` — delays agent startup.
- Print to stdout/stderr. Use `api.log(...)`.
- Write outside `api.user_data_dir` without going through
  `permissions.require_access`.
- Communicate with pyagent core through filesystem side channels. If a
  capability is missing, the API needs to grow.
