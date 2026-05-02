# Pyagent

Pyagent is a simple example of an agent in python. You can chat with it
and it can take action on your computer.

## Setup

Pyagent is pre-configured for models from Anthropic, Google, and OpenAI. The
only setup required is to:

- Install `pyagent` (this project)
- Put your API key in the appropriate environment variable

### Install

From a clone of the repo:

```
pip install .
```

Or, if you plan to hack on it, an editable install so your edits take effect
without reinstalling:

```
pip install -e .
```

Requires Python 3.11+.

## Environment variables

Standard platform environment variables. Set the one matching the provider
you intend to use; the others can stay unset.

| Variable | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `--model anthropic` | |
| `OPENAI_API_KEY` | `--model openai` | |
| `GEMINI_API_KEY` | `--model gemini` | `GOOGLE_API_KEY` is accepted as a fallback. |

## Run the agent

Once you've installed (using `pip`):
```
pyagent
```

Run `pyagent --help` for the full list of flags (model selection, session
resume, prompt-file overrides, and the toggles below).

## Selecting a model

Pass `--model` as `provider` or `provider/model-name`. With just a provider,
the client's built-in default is used.

```
pyagent --model anthropic                      # claude-sonnet-4-6 (default)
pyagent --model anthropic/claude-opus-4-7      # pick a specific Claude model
pyagent --model openai                         # gpt-4o
pyagent --model openai/gpt-4o-mini
pyagent --model gemini                         # gemini-2.5-flash
pyagent --model gemini/gemini-2.5-pro
```

If `--model` is omitted, pyagent picks one in this order:

1. **`default_model`** in `config.toml` (e.g. `default_model = "openai"`).
2. **Auto-detect** from the API-key env vars: `ANTHROPIC_API_KEY` →
   `OPENAI_API_KEY` → `GEMINI_API_KEY`/`GOOGLE_API_KEY`. The first that's
   set wins.
3. If neither is available, pyagent exits with a pointed error telling
   you which env vars it looks for.

The session header prints the resolved provider/model so you can confirm
what was picked.

Make sure the matching API key from the table above is set in your
environment before launching.

### Switching models mid-session

At the prompt, type `/model <spec>` to swap the running agent's LLM
client without restarting:

```
> /model openai/gpt-4o
> /model anthropic
> /model planner          # role name (see Roles section)
```

The swap takes effect on the next API call. The status footer updates
to reflect the new model. A bad spec leaves the existing client in
place and prints a warning. Subagents are not affected — each child
keeps the model it was spawned with.

### Talking to a busy agent

The input field stays alive while the agent works. Anything you type
while a turn is running queues up; each submitted line gets a `>>`
echo, and the status footer surfaces queue depth (`queued: 2 (next:
"now run the tests")`). When the turn finishes, the head of the queue
becomes the next user prompt automatically.

```
> /queue              # show queued entries
> /queue clear        # flush the queue without sending anything
> /queue pop          # drop the most recent typed entry (likely a typo)
```

`/tasks` prints the agent's current checklist (also reflected in the
footer as `3/7 · "writing migration"`). The model maintains it via
`add_task` / `update_task` for genuine multi-step work.

Press **Esc** while the agent is busy to cancel the in-flight turn —
this also discards any queued input (queued lines tied to the
cancelled turn are usually stale). Esc is a no-op when the agent is
idle.

## Design

### The agent loop

The loop lives in `Agent.run()` and does the same thing every turn:

- Append the user's prompt to the conversation.
- Send the conversation, the system prompt, and the tool schemas to the model.
- Append the assistant's reply to the conversation.
- If the reply has no tool calls, return its text — turn over.
- Otherwise, run each requested tool, append the results as a `tool_results`
  message, and loop back to the model.

The model decides when it's done by simply not asking for any more tools.

### Tools

Tools give the agent hands. The LLM will request tool calls, and
the agent will invoke them and return the results.

> A tool is simply a python function that returns a `str`.

The following is a table of the built-in tools.

| Tool | What it does |
| --- | --- |
| `read_file` | Read a text file, optionally a line range. Auto-truncates above 2000 lines. |
| `write_file` | Write content to a file, overwriting any existing one. |
| `list_directory` | List the entries in a directory; directories are suffixed `/`. |
| `grep` | Search for a regex pattern in a file or recursively across a directory. |
| `execute` | Run a shell command (60s timeout). A small regex blocklist refuses obviously dangerous patterns. |
| `fetch_url` | HTTP GET a URL. Always saves the raw body to a session attachment; by default also returns markdown of the article body inline. `format="void"` skips the inline conversion for triage / batch fetches. |
| `list_plugins` | List the plugins currently loaded. Self-improvement helper. |

HTML tools (`html_to_md` / `html_select`) come from the bundled
`html-tools` plugin and operate on saved attachments (or any local
HTML file). Memory tools (`read_ledger`, `write_ledger`, `add_memory`)
come from the bundled `memory-markdown` plugin; semantic recall
(`recall_memory`) comes from the bundled `memory-vector` plugin. Other
bundled plugins (`code-mapper`, `web-search`, `claude-code-cli`)
register additional tools — all ship default-enabled. See "Plugins"
below for the full list.

File tools resolve paths and refuse anything outside the workspace unless the
human approves at a prompt. See `pyagent/permissions.py`. The user's pyagent
config dir is pre-approved so the agent can read/write its persona and
plugin data without prompting.

### Soul and the system prompt

The system prompt is assembled at the start of every turn from a few
markdown files (see `pyagent/prompts.py`). Edit any of them and the change
takes effect on the next turn — no restart needed.

- **`SOUL.md`** — who the agent is. Voice, persona, core directives, the
  things that don't change between tasks.
- **`TOOLS.md`** — *when and how* to use the tools. The parameter schemas
  are sent separately; this file is judgment, not API reference.
- **`PRIMER.md`** — safety and behavior rails. Workspace boundaries, what
  needs explicit consent, verifying before recommending.
SOUL/TOOLS/PRIMER live in the user's pyagent config dir (Linux:
`~/.config/pyagent/`, macOS: `~/Library/Application Support/pyagent/`,
Windows: `%APPDATA%\pyagent\`). On first run, the bundled defaults from the
package are copied in; after that, edits to the config-dir copies take
effect immediately. A file with the same name in the current working
directory takes precedence — handy for per-project SOUL/TOOLS overrides or
for hacking on the prompts inside the pyagent repo itself.

Paths can be overridden with `--soul`, `--tools`, `--primer`.

### Memory

Long-term memory is provided by two bundled plugins (see "Plugins"
below). `memory-markdown` is the storage backend and exposes
`read_ledger`, `write_ledger`, and `add_memory` — backed by markdown
files under `<config-dir>/plugins/memory-markdown/` (`USER.md`,
`MEMORY.md`, plus a `memories/` directory of individual entries).
`add_memory` is the preferred tool for new entries: it writes the
memory body and updates the MEMORY index in one call.

`memory-vector` layers semantic recall on top via `recall_memory`,
indexing the same files with fastembed so the agent can find
memories by meaning rather than by exact filename. The two plugins
are loosely coupled — `memory-vector` reads from `memory-markdown`'s
data dir at runtime; if `memory-markdown` is disabled, `recall_memory`
just reports that nothing is indexed.

`memory-markdown` also contributes prompt sections that auto-load
USER ledger content and a brief MEMORY index into every system
prompt. Drop both plugins from `built_in_plugins_enabled` in
`config.toml` to remove memory entirely — the agent will have no
memory tools and no memory prose.

Memory work is meant to happen **organically**, mid-conversation: when the
agent learns a preference, a convention, or something genuinely worth
remembering, it updates the appropriate ledger then and there.

To wipe a plugin's data: `pyagent-plugins reset memory-markdown`.

Conversation history and large tool outputs are persisted separately, under
`.pyagent/sessions/<session-id>/`. Resume a session with `pyagent --resume
<session-id>`, or `pyagent --resume` with no value to list them.

## Skills

Skills are bundles of instructions (and optional helper scripts) the agent
can load on demand. The system prompt advertises *that* a skill exists; the
agent calls `read_skill(<name>)` when its description matches what the user
is asking for, and the skill's body lands in context for the rest of the
session.

A skill is a directory with a `SKILL.md`:

```
example-skill/
  SKILL.md          # YAML-ish frontmatter + instructions for the agent
  scripts/          # optional — CLI helpers the agent invokes via the shell tool
    cli.py
```

Frontmatter fields:

| Field | Purpose |
| --- | --- |
| `name` | Catalog identifier the agent uses to load the skill. |
| `description` | One-line summary; the agent uses this to decide relevance. |

Skills don't register Python tools. Helper scripts under `scripts/` are
invoked via the regular shell tool, so they go through the same Bash safety
checks as any other command.

Discovery order (later wins, project-local overrides everything):

1. `<package>/skills/<name>/` — bundled with pyagent
2. `<config-dir>/skills/<name>/` — user-installed
3. `./.pyagent/skills/<name>/` — project-local

The catalog re-renders before every model call, so a skill you (or the
agent) just authored shows up on the next call — no restart needed.

### Bundled skills

Bundled skills load directly from the package — no install step, no copy on
disk. Upgrading pyagent updates them for free.

**`write-skill`** and **`write-plugin`** are enabled out of the box.
The rest are opt-in to keep the catalog tight. Enable additional
bundled skills by listing their names in `<config-dir>/config.toml`:

```toml
built_in_skills_enabled = ["write-skill", "write-plugin", "flight-tracker"]
```

Setting `built_in_skills_enabled` replaces the default list, so include
every bundled skill you want available — `write-skill` and
`write-plugin` included.

Run `pyagent-skills list` to see each bundled skill's name, description,
and current `[enabled]` / `[disabled]` state.

| Skill | What it does |
| --- | --- |
| `write-skill` | Authoring guide — load this when you want the agent to write a new skill for you. **Enabled by default.** |
| `write-plugin` | Authoring guide for plugins — manifest schema, PluginAPI surface, hooks. Load when creating or modifying a plugin. **Enabled by default.** |
| `aviation-weather` | METARs, TAFs, PIREPs, AFD, AIRMETs/SIGMETs around an airport. Uses aviationweather.gov; no key needed. |
| `flight-tracker` | Live aircraft state vectors near a point or by ICAO24 hex via OpenSky. Anonymous works; OAuth2 client credentials unlock more. |
| `faa-registry` | Look up FAA aircraft registry records (US tail numbers) by N-number, owner, or make/model. |

To customize a bundled skill, copy its directory into `<config-dir>/skills/`
or `./.pyagent/skills/` and edit. The override takes precedence regardless
of `built_in_skills_enabled` — user-installed and project-local tiers are
never gated by config.

`pyagent-skills uninstall <name>` removes a user- or project-local copy.
Bundled skills can't be uninstalled (they ship with the package); to keep
one out of the catalog, just leave it out of `built_in_skills_enabled`.

## Plugins

Plugins extend pyagent at runtime — they can register tools,
contribute prompt sections, and observe the conversation loop. Unlike
skills (which are passive markdown), plugins are active code that
runs alongside the agent.

A plugin is a directory with `manifest.toml` + `plugin.py` (drop-in)
or an installed Python package declaring an entry point in the
`pyagent.plugins` group. Discovery is three-tier (later wins on name
collision, same as skills):

1. **Bundled** — `pyagent/plugins/<name>/`. Filtered against
   `built_in_plugins_enabled` in `config.toml`.
2. **Entry-point installed** — `pip install pyagent-foo`.
3. **Drop-in** — `<config-dir>/plugins/<name>/` (user) or
   `./.pyagent/plugins/<name>/` (project).

`pyagent-plugins list` shows what's discovered, with override
warnings when a higher-tier plugin shadows a lower-tier one.
`pyagent-plugins reset <name>` wipes a plugin's `<config-dir>/plugins/<name>/`
data dir.

### Bundled plugins

| Plugin | What it provides | Default enabled? |
| --- | --- | --- |
| `memory-markdown` | Markdown ledger storage — `read_ledger`, `write_ledger`, `add_memory`, plus USER/MEMORY prompt sections. Root-only (does not load in subagents). See "Memory" above. | yes |
| `memory-vector` | Semantic recall (`recall_memory`) over `memory-markdown`'s files via fastembed. Root-only. | yes |
| `html-tools` | `html_to_md` / `html_select` — convert or query saved HTML attachments (or any local HTML file). | yes |
| `code-mapper` | `map_code` / `probe_grammar` — tree-sitter symbol map for source files (Python in v1; multi-language ready). | yes |
| `web-search` | `web_search` / `web_search_instant` — DuckDuckGo-backed list search and instant answers. | yes |
| `claude-code-cli` | `claude_code_cli` — pipe a prompt into Anthropic's `claude -p`. Self-disables when `claude` isn't on PATH. | yes |
| `echo-plugin` | Test/demo provider that echoes the most recent user message. Exercises the plugin → llm-router wiring without spending tokens. | yes |

To remove a plugin from the catalog, set `built_in_plugins_enabled`
in `config.toml` to the list of names you want kept. An empty list
disables every bundled plugin.

### Authoring a plugin

The full design and API surface live in `docs/plugin-design.md` and
`docs/plugin-feature-summary.md`. Quick start:

```python
# ~/.config/pyagent/plugins/hello/plugin.py
def register(api):
    def hello(name: str) -> str:
        """Say hi."""
        return f"hi, {name}"
    api.register_tool("hello", hello)
```

```toml
# ~/.config/pyagent/plugins/hello/manifest.toml
name = "hello"
version = "0.1.0"
description = "Trivial example."
api_version = "1"
[provides]
tools = ["hello"]
```

Restart pyagent. The LLM has a `hello` tool. See
`pyagent/plugins/memory_markdown/` for a complete bundled example
exercising tools, prompt sections, and lifecycle hooks.

## Configuration

Pyagent reads config from two tiers, both optional:

- `<config-dir>/config.toml` — user tier (per-user defaults)
- `./.pyagent/config.toml` — project tier (per-repo overrides)

Effective config is `defaults < user < project`, deep-merged. A missing
file at any tier is fine — bundled defaults apply. The `pyagent-config`
CLI inspects and initializes the user-tier file:

```
pyagent-config show          # effective merged config (defaults + overrides)
pyagent-config defaults      # bundled defaults as a commented-out template
pyagent-config init          # write the template to config.toml if absent
```

`init` never overwrites; pass `--force` if you really want to start over.
The written template is fully commented out, so the file's presence does
not change behavior — uncomment lines to override defaults.

### Roles (named subagent models)

Define `[models.<name>]` tables in `config.toml` to give the
orchestrator addressable subagent presets. The orchestrator then calls
`spawn_subagent(model="planner")` (or any other defined role name)
instead of repeating raw provider strings in every spawn. Roles also
appear as targets for the `/model` slash command.

```toml
[models.planner]
model = "anthropic/claude-opus-4-7"
description = "Deep reasoning, multi-step planning."
system_prompt = """
You are a planner. Break tasks into steps before recommending edits.
"""
tools = ["read_file", "grep", "list_directory"]   # optional allowlist
meta_tools = false                                # leaf role, can't fan out
```

| Field | Required | Purpose |
| --- | --- | --- |
| `model` | yes | provider/model string in the same form as `--model`. |
| `description` | yes | One-line summary; the orchestrator uses this to decide when to spawn this role. |
| `system_prompt` | no | Default subagent persona body, layered onto SOUL/TOOLS/PRIMER (use `system_prompt_path` instead for longer prose; mutually exclusive). |
| `tools` | no | Allowlist that narrows the default tool set. Absent = full default. |
| `meta_tools` | no | Default `true`. Set `false` for leaves that should not themselves spawn subagents. |

Roles render into a live "Available subagent models" catalog that the
orchestrator sees in its system prompt. `/model <role-name>` and
`spawn_subagent(model=...)` use the same lookup — role names win over
raw provider strings.

## Managing sessions

Conversation history lives under `./.pyagent/sessions/<session-id>/`. The
`pyagent-sessions` CLI inspects and cleans it up:

```
pyagent-sessions list                          # all sessions, newest first
pyagent-sessions delete <id>                   # remove one session
pyagent-sessions delete --all                  # remove every session in this project
pyagent-sessions prune --older-than 30         # delete anything inactive 30+ days
pyagent-sessions prune --keep 10               # keep newest 10, drop the rest
```

`prune` defaults to dry-run; pass `--no-dry-run` to actually delete.

## Resetting

Pyagent's reset flags overwrite files in `<config-dir>` with the bundled
defaults. They never touch your workspace (`./.pyagent/`) — sessions and
project-local skills are yours to manage.

| Flag | Effect |
| --- | --- |
| `--reset-soul` / `--reset-tools` / `--reset-primer` | Overwrite the spec doc with the bundled default. |
| `--reset-skills` | Remove every user-installed skill under `<config-dir>/skills/`. |
| `--reset-all` | All of the above, with one consolidated confirmation. |
| `--yes` / `-y` | Skip the confirmation prompt for destructive resets. |

The destructive reset (`--reset-skills`) prompts before doing anything;
spec-doc resets don't, since those are pure revert-to-ship-state.

Plugin data lives at `<config-dir>/plugins/<name>/`; wipe it with
`pyagent-plugins reset <name>` (e.g. `pyagent-plugins reset memory-markdown`
to clear USER and MEMORY ledgers).
