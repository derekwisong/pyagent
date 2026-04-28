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
| `fetch_url` | HTTP GET a URL and return status + body. |
| `read_ledger` | Read a ledger by name (`USER` or `MEMORY`). The path is resolved automatically. |
| `write_ledger` | Overwrite a ledger by name. The path is resolved automatically. |

File tools resolve paths and refuse anything outside the workspace unless the
human approves at a prompt. See `pyagent/permissions.py`. The user's pyagent
config dir is pre-approved so the agent can read/write its persona and
ledger files without prompting.

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
- **`USER.md`** — per-user notes about the person being helped. Auto-loaded
  into the prompt if it exists. Seeded from `USER.md.template` on first
  read/write.

These files live in the user's pyagent config dir (Linux:
`~/.config/pyagent/`, macOS: `~/Library/Application Support/pyagent/`,
Windows: `%APPDATA%\pyagent\`). On first run, the bundled defaults from the
package are copied in; after that, edits to the config-dir copies take
effect immediately. A file with the same name in the current working
directory takes precedence — handy for per-project SOUL/TOOLS overrides or
for hacking on the prompts inside the pyagent repo itself.

Paths can be overridden with `--soul`, `--tools`, `--primer`. The two
ledgers (`USER.md`, `MEMORY.md`) are accessed exclusively through the
`read_ledger` / `write_ledger` tools, which encapsulate the path resolution
so the agent's notebook follows the user across working directories
instead of getting scattered into every project folder.

### Memory and self updating

`MEMORY.md` is the agent's long-term notebook. It lives in the config dir
alongside `USER.md` and is *not* auto-loaded into the system prompt — the
agent reads and edits it via the ledger tools. `SOUL.md` instructs the
agent to keep it tidy: record what's truly memorable, prune by removing
whole entries rather than blending them, and never wipe the file wholesale
without an explicit ask.

Memory work is meant to happen **organically**, mid-conversation: when the
agent learns a preference, a convention, or something genuinely worth
remembering, it updates the appropriate ledger then and there.

An optional **end-of-session pass** is available as a safety net: launch
with `--memory-pass-on-exit` and the CLI will, on exit (Ctrl+D or Ctrl+C
at the prompt), ask the agent to sweep the conversation for anything
that should have been recorded but wasn't, and update the ledgers. The
pass only fires when the session actually added new turns; resumed-but-
idle sessions exit immediately. A second Ctrl+C during the pass skips
it cleanly. It's off by default — the expectation is that the agent
records memory organically mid-conversation, so this is for rare
"final sweep" cases.

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

Only **`write-skill`** is enabled out of the box. The rest are opt-in to
keep the catalog tight. Enable additional bundled skills by listing their
names in `<config-dir>/config.toml`:

```toml
built_in_skills_enabled = ["write-skill", "flight-tracker"]
```

Setting `built_in_skills_enabled` replaces the default list, so include
every bundled skill you want available — `write-skill` included.

Run `pyagent-skills list` to see each bundled skill's name, description,
and current `[enabled]` / `[disabled]` state.

| Skill | What it does |
| --- | --- |
| `write-skill` | Authoring guide — load this when you want the agent to write a new skill for you. **Enabled by default.** |
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

## Configuration

Pyagent reads its config from `<config-dir>/config.toml`. A missing file is
fine — bundled defaults apply. The `pyagent-config` CLI inspects and
initializes the file:

```
pyagent-config show          # effective merged config (defaults + overrides)
pyagent-config defaults      # bundled defaults as a commented-out template
pyagent-config init          # write the template to config.toml if absent
```

`init` never overwrites; pass `--force` if you really want to start over.
The written template is fully commented out, so the file's presence does
not change behavior — uncomment lines to override defaults.

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
| `--reset-user` | Overwrite `USER.md` (preferences). |
| `--reset-memory` | Overwrite `MEMORY.md` (long-term memory). |
| `--reset-skills` | Remove every user-installed skill under `<config-dir>/skills/`. |
| `--reset-all` | All of the above, with one consolidated confirmation. |
| `--yes` / `-y` | Skip the confirmation prompt for destructive resets. |

The destructive resets (USER, MEMORY, skills) prompt before doing anything;
spec-doc resets don't, since those are pure revert-to-ship-state.
