# Migrating Memory to a Plugin

The first real test of the plugin API: lift the existing memory system
out of pyagent's core and into a bundled `memory-markdown` plugin
without changing observable behavior.

This doc is the staged plan, the cutover criteria, and the rollback.

## Why memory first

- It's small (~60 lines of tool code, plus prose in SOUL.md).
- It has every shape a non-trivial plugin needs: tools, prompt
  contribution, lifecycle hooks, persistent on-disk state.
- If the plugin API can't express markdown memory cleanly, the API
  needs more work *before* anyone ships a real third-party plugin.
- Once it's a plugin, swapping in a vector or sqlite backend is "drop
  in a different plugin" — no core changes.

## What the memory system currently consists of

| Piece | Where it lives | What it does |
| --- | --- | --- |
| `read_ledger`, `write_ledger` tools | `pyagent/tools.py` | The agent's only sanctioned way to touch USER.md / MEMORY.md |
| `_LEDGERS` mapping | `pyagent/tools.py` | Maps logical names ("USER", "MEMORY") to filenames |
| `MEMORY.md`, `USER.md` defaults | `pyagent/defaults/` | Bundled seed templates |
| Path resolution | `pyagent/paths.py` (`paths.resolve(...)`) | Resolves `<config-dir>/MEMORY.md`, seeded on first read |
| Permission gate | `pyagent/permissions.py` (config-dir pre-approve in `cli.py`) | So writes don't prompt |
| SOUL prose | `pyagent/defaults/SOUL.md` ("The Ledgers" section) | Tells the agent how to use the ledgers |
| End-of-session sweep | `pyagent/cli.py` (`_END_OF_SESSION_PROMPT`, `--memory-pass-on-exit`) | Optional final-pass extraction. **Removed entirely in this migration** — see "Sweep removal" below. |
| `--reset-user`, `--reset-memory` flags | `pyagent/cli.py` | Wipe the ledger files |

The migration touches all of these. Most relocate; a few need the
plugin API to land first.

## What the bundled plugin owns after migration

```
pyagent/plugins/memory_markdown/
  manifest.toml        # see schema below
  plugin.py            # registers tools, prompt section, hooks
  defaults/
    MEMORY.md          # seed template (moved from pyagent/defaults/)
    USER.md            # seed template
    PROMPT.md          # SOUL "The Ledgers" prose, lifted out of SOUL.md
```

What stays in core:

- `pyagent/paths.py` — still resolves config-dir paths; the plugin
  uses `api.user_data_dir` for persistent ledger storage.
- `pyagent/permissions.py` — pre-approval of config-dir continues; the
  plugin's tools share the same gate as built-in tools.
- The agent loop, the prompt builder (extended with cache-breakpoint
  support), the rest of SOUL.md.

## Manifest

```toml
# pyagent/plugins/memory_markdown/manifest.toml
name = "memory-markdown"
version = "0.1.0"
description = "Markdown-file memory backend (the original ledger system)."
api_version = "1"

[provides]
tools = ["read_ledger", "write_ledger"]
prompt_sections = ["memory-guidance", "user-ledger"]

[load]
# Default true. memory-markdown does full-overwrite writes which are
# not parallel-safe; we set false here so subagents skip it. The root
# agent owns the ledgers; subagents that need to read them go through
# the parent (a future feature) or, today, just don't touch them.
in_subagents = false
```

The `[load] in_subagents = false` is a deliberate choice. The existing
memory system was never parallel-safe (full overwrite, no locking).
Rather than retrofit locking, the plugin opts out of subagents — root
keeps the ledgers, subagents skip. A future `memory-sqlite` or
`memory-vector` plugin with proper concurrency can flip this back to
`true`.

## Stages

### Stage 1: land the plugin loader (no behavior change)

Add the plugin discovery and loading machinery without yet migrating
memory.

1. Create `pyagent/plugins.py` with `PluginAPI`, `PromptContext`,
   `discover()`, `load()`, `apply_to(agent, system_builder)`.
2. Extend `pyagent/prompts.py` to emit cache-breakpoint markers so
   volatile sections (set per-plugin via `register_prompt_section(...,
   volatile=True)`) live after the last `cache_control` marker.
   Update `pyagent/llms/anthropic.py` to honor the breakpoint
   structure with up to 4 markers; `openai.py` and `gemini.py` need to
   produce correct output (caching where supported, no-op where not).
3. Wire the loader into `agent_proc._bootstrap`:
   - `register(api)` runs during bootstrap, bounded.
   - After agent + builder constructed, `state.send("ready")` sent,
     and `io_thread.start()` called, fire `on_session_start` for each
     plugin (5s deadline). Order matters: the v1 review caught that
     firing this earlier hangs bootstrap silently.
4. Wire conversation hooks into `Agent.run`:
   - `after_assistant_response` after each `on_text` callback fires.
   - `before_tool_call` and `after_tool_call` around `_execute_tool`.
   - All bounded to 200ms with deadline-and-skip semantics.
5. Wire the missing-tool error: `Agent._route_tool` formats a rich
   error string (current catalog + originating-plugin suggestion from
   manifest `[provides]`) when `name not in self.tools`, instead of
   the bare `KeyError` from `_execute_tool`.
6. Add `pyagent-plugins` CLI: `list`, `validate <path>`. Plus two
   built-in agent tools: `list_plugins`, `inspect_plugin`.
7. Add config keys: `built_in_plugins_enabled = []` (empty default)
   and `[plugins.<name>]` deep-merge support.
8. Tests: discover bundled, validate manifests including `[provides]`
   conformance, register a test plugin from a temp dir, assert tools
   land on the agent, assert volatile sections don't bust the cache,
   assert hook timeouts don't wedge a turn.

After stage 1: zero behavior change. The agent loads no plugins
because `built_in_plugins_enabled` is empty.

### Stage 2: ship `memory-markdown` as a bundled plugin

Move the existing system into the plugin while keeping it the default.

1. Create `pyagent/plugins/memory_markdown/{manifest.toml,plugin.py,defaults/}`.
2. Copy (don't move yet) `MEMORY.md`, `USER.md` into the plugin's
   `defaults/`. The bundle still ships them in `pyagent/defaults/` for
   one release so resets don't break.
3. The plugin registers `read_ledger` and `write_ledger` — same names,
   same signatures. Persistence path is `api.user_data_dir` (resolves
   to `<config-dir>/plugins/memory-markdown/`). The plugin starts
   fresh at the new path; existing `<config-dir>/MEMORY.md` and
   `<config-dir>/USER.md` files are left untouched on disk as
   orphans. On first session start after the migration, the plugin
   emits a one-time `info` event:
   `"memory-markdown: legacy ledger files at <paths> are no longer
   used. Delete them manually if you wish."`
   This avoids any code path that deletes user data.
4. Remove the unconditional `_add("read_ledger", ...)` and
   `_add("write_ledger", ...)` calls from
   `agent_proc._register_tools`. Now those tools only exist when the
   `memory-markdown` plugin is enabled.
5. Default `built_in_plugins_enabled = ["memory-markdown"]`. Existing
   users get the plugin active automatically.
6. The plugin contributes **two** prompt sections, both
   `volatile=False`:
   - `"memory-guidance"` — the "how to use the ledgers" instructional
     prose, lifted from SOUL.md into `defaults/PROMPT.md`.
   - `"user-ledger"` — the contents of USER.md, splatted into every
     system prompt. **Critical**: pre-plugin pyagent auto-loads
     USER.md into the system prompt via `SystemPromptBuilder.build()`
     (see `pyagent/prompts.py:86`). The plugin must preserve this so
     preferences/conventions surface without the agent calling
     `read_ledger("USER")` for every basic fact. MEMORY.md stays
     recall-based (agent calls `read_ledger("MEMORY")` on demand).

   Once the plugin owns USER auto-load, remove the
   `paths.resolve("USER.md") / read_text()` block from
   `pyagent/prompts.py:86-88`. SOUL.md keeps a one-line pointer
   ("Memory is provided by a plugin; see its prompt section for
   usage.").
7. The end-of-session sweep is **removed entirely**, along with the
   `--memory-pass-on-exit` CLI flag. See "Sweep removal" below for
   the rationale and the future story.

After stage 2: identical user experience for anyone who hasn't
disabled memory. New capability: `built_in_plugins_enabled = []`
disables memory entirely (tools gone, prompt section gone, ledger
files left untouched on disk).

### Stage 3: clean up duplication and remove core memory code

Once stage 2 has shipped and stuck for a release:

1. Remove `read_ledger` / `write_ledger` from `pyagent/tools.py`. The
   plugin owns the canonical implementation; the core copies were
   only there for migration.
2. Remove `MEMORY.md` / `USER.md` from `pyagent/defaults/`. The plugin
   ships them.
3. Remove `--reset-memory` / `--reset-user` from the CLI; replace with
   a generic `pyagent-plugins reset <name>` (the plugin gets a
   `reset()` callback the CLI invokes; for `memory-markdown`, this
   restores the bundled templates). Keep deprecated flags for one
   release.
4. Remove the SOUL.md "The Ledgers" section entirely; it's been a
   pointer for a release, the plugin's prompt section has been doing
   the actual work.

After stage 3: the only mention of "memory" in core is in the plugin
loader's hook surface and in SOUL.md as a line saying "memory comes
from plugins."

## Sweep removal

The original system had an opt-in `--memory-pass-on-exit` flag that,
on session end, sent a final LLM prompt asking the agent to "review
this conversation and save anything that should have been recorded."
It was off by default — SOUL.md tells the agent to record memory
organically mid-conversation, and the flag was a safety net.

This flag is **removed in the migration with no plugin replacement**.
Reasons:

- A clean port of the sweep would have the plugin write a sentinel
  file in `session_data_dir` for the CLI to pick up at shutdown — i.e.
  plugin-CLI coordination through filesystem side channels. That's
  the exact anti-pattern the plugin boundary exists to prevent.
- A real port needs the v2 runtime APIs (`api.create_agent` for the
  sweep's LLM call, `api.deliver` for any user-facing notification,
  potentially a longer shutdown deadline). Those APIs aren't in v1.
- Building the sweep as a v1-shaped feature now would lock in a
  shape we'd regret once v2 lands. Better to remove and rebuild
  once the runtime supports it.

When the v2 runtime ships, a separate `memory-sweep` plugin can
provide this — composing on top of `memory-markdown` rather than
being baked into it. The bundled storage plugin stays simple.

## Compatibility surface

What must not change across the migration:

- **Tool names.** `read_ledger` / `write_ledger` keep their names and
  signatures. Saved sessions reference these by name in tool calls;
  renaming would break resume.
- **Prompt content semantics.** "The ledgers are kept, not destroyed"
  and the rest of the SOUL guidance must still reach the agent. It
  arrives via a plugin prompt section instead of being inline in
  SOUL.md.
- **Reset flags.** `--reset-memory` / `--reset-user` continue to work
  through stage 2. Removed in stage 3 with prior deprecation warning.

What's allowed to change:

- **Ledger file paths.** Move from `<config-dir>/MEMORY.md` /
  `<config-dir>/USER.md` to
  `<config-dir>/plugins/memory-markdown/MEMORY.md` /
  `<config-dir>/plugins/memory-markdown/USER.md`. The plugin starts
  fresh at the new path; legacy files become orphans on disk. The
  plugin emits a one-time `info` event pointing them out so users
  know they can be deleted manually.
- The CLI flag `--memory-pass-on-exit` is removed entirely (no
  deprecation alias). The bundled plugin doesn't replace it.
- Internal imports — anything importing `read_ledger` from
  `pyagent.tools` will break in stage 3. We grep the repo before the
  stage 3 PR to find any internal callers.

## Cross-backend swap (the real test)

The reason the plugin API exists is so a user can replace
`memory-markdown` with `memory-vector` (or whatever). When that swap
happens mid-life-of-a-saved-session, the new plugin won't expose
`read_ledger` / `write_ledger` — it'll have its own tool names. The
graceful behavior:

- The conversation history is kept intact. Historical `read_ledger`
  calls in the transcript are facts about what happened, not promises
  about current state.
- When the LLM tries to call `read_ledger` after the swap, the rich
  missing-tool error fires:
  ```
  <tool 'read_ledger' is not currently available.
  Available tools: recall_memory, save_fact, ...
  This tool was provided by plugin 'memory-markdown' (currently
  disabled or removed). To restore: enable the plugin in config.toml.>
  ```
- The current tool catalog renders in the system prompt every turn,
  so the LLM has both the missing-tool error and the new catalog and
  adapts within a turn.
- The new plugin's prompt section explains its own tools. The LLM
  sees the new shape on its first turn after restart.

This makes long-running sessions safe — Telegram or Discord bridges
that keep a session open for months can have their memory backend
swapped without breaking the conversation.

## Cutover criteria

Stage 1 ships when:

- A test plugin can register a tool and have it appear on the agent.
- A test plugin can register a prompt section that appears in
  `system_builder.build()`.
- A volatile section's content changing turn-to-turn does not change
  the bytes inside the cache_control span.
- Hook timeouts log + skip without wedging the agent loop.
- `on_session_start` / `on_session_end` fire at the right points.
- `[provides]` mismatches (plugin registers more or less than declared)
  fail the plugin loud at load.
- Manifest validation rejects malformed manifests with a clear message.
- Calling a nonexistent tool returns the rich missing-tool error.
- `pyagent-plugins list` shows tier, enabled state, declared
  `[provides]`, and shadowing warnings.
- `pyagent-plugins validate <path>` works on a candidate plugin
  directory and reports load failures.
- Built-in agent tools `list_plugins` and `inspect_plugin` work and
  return correct data.

Stage 2 ships when:

- With `memory-markdown` enabled (the default), the agent sees
  `read_ledger` / `write_ledger` and the ledger prose, identical to
  pre-migration. Behavior is byte-for-byte equivalent on a fresh
  install.
- With `built_in_plugins_enabled = []`, the agent has no ledger tools
  and no ledger prose. The agent still runs.
- Resume works: a session created pre-migration still loads, and the
  agent can call `read_ledger` (because the plugin is enabled by
  default).
- A swap test passes: enable a stub `memory-other` plugin, disable
  `memory-markdown`, resume a session that called `read_ledger`. The
  agent gets the rich missing-tool error and continues — no crash.
- `--memory-pass-on-exit` is removed; passing it produces an
  unrecognized-option error from click. The release notes call this
  out.

Stage 3 ships when:

- No internal code outside the plugin imports `read_ledger`/
  `write_ledger`.
- The deprecation period for the CLI flags has elapsed.
- A "stress" run — disable the plugin, run a typical session — shows
  the agent gracefully reports "I don't have a memory system in this
  configuration" if asked rather than calling a missing tool.

## Rollback

If stage 2 produces unexpected behavior in production:

1. Set `built_in_plugins_enabled = ["memory-markdown"]` (the default,
   no change).
2. If the plugin itself is at fault, the user can copy the bundled
   `memory_markdown/` directory into `<config-dir>/plugins/`, edit
   `plugin.py` to patch, and the override takes precedence.
3. As a last resort, set `[plugins.memory-markdown] enabled = false`
   to fully disable, then revert to the prior pyagent release.

## Open questions

- **Where does the memory prompt section sit, exactly?** Default
  proposal: `position = "after_primer"`, `volatile = False`. The
  prose is static; volatile would only matter for a future
  vector-recall plugin's "currently-relevant memories" section.
- **Can the agent author its own memory plugin mid-session?** Yes —
  it can write to `<config-dir>/plugins/<name>/`, run `pyagent-plugins
  validate <path>` via the shell tool, and ask the user to restart.
  v2 hot-reload would close that loop without restart.
- **Should `memory-markdown` ever be parallel-safe?** Probably not —
  if a user wants multi-agent memory, the right move is a
  `memory-sqlite` or `memory-vector` plugin with proper concurrency.
  `memory-markdown` stays simple, opts out of subagents.
