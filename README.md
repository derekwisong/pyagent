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

As a safety net, the CLI also runs a single **end-of-session pass** when
you exit (Ctrl+D or Ctrl+C at the prompt). It asks the agent to sweep the
conversation for anything that should have been recorded but wasn't, and
to update the ledgers. If the agent has already kept things tidy, it will
say so and exit. The pass only fires when the session actually added new
turns; resumed-but-idle sessions exit immediately. A second Ctrl+C during
the pass skips it cleanly.

To skip the pass entirely (throwaway chats, sensitive conversations,
scripted runs), launch with `--no-memory-pass-on-exit`.

Conversation history and large tool outputs are persisted separately, under
`.pyagent/sessions/<session-id>/`. Resume a session with `pyagent --resume
<session-id>`, or `pyagent --resume` with no value to list them.

## Skills

Skills are bundles of instructions (and optional helper tools) the agent can
load on demand. The system prompt advertises *that* a skill exists; the agent
calls `use_skill(<name>)` when its description matches what the user is
asking for, and the skill's body lands in context for the rest of the
session.

A skill is a directory with a `SKILL.md`:

```
example-skill/
  SKILL.md         # YAML-ish frontmatter + instructions for the agent
  tools.py         # optional, exports a TOOLS = {"name": fn, ...} dict
```

Frontmatter fields:

| Field | Purpose |
| --- | --- |
| `name` | Catalog identifier the agent uses to load the skill. |
| `description` | One-line summary; the agent uses this to decide relevance. |
| `tools` | Optional. Path (relative to `SKILL.md`) of a Python file whose `TOOLS` dict gets registered with the agent on first activation. |

Discovery order (first wins, local overrides everything):

1. `./.pyagent/skills/<name>/SKILL.md` — project-local
2. `<config-dir>/skills/<name>/SKILL.md` — user-installed

Skills that ship a `tools` module run arbitrary Python on activation, so the
first `use_skill(<name>)` call in a session prompts the human for one-time
approval. Pure-instructional skills (no `tools`) skip the prompt — their
effects route through the existing tool layer with its own permissions.

### Bundled skills

Run `pyagent-skills list` to see what's bundled and what's installed.
Install a bundled skill into your config dir with:

```
pyagent-skills install aviation-weather
pyagent-skills install flight-tracker
pyagent-skills install faa-registry
```

Bundled skills:

| Skill | What it does |
| --- | --- |
| `write-skill` | Authoring guide — install this if you want the agent to be able to write new skills for you. |
| `aviation-weather` | METARs, TAFs, PIREPs, AFD, AIRMETs/SIGMETs around an airport. Uses aviationweather.gov; no key needed. |
| `flight-tracker` | Live aircraft state vectors near a point or by ICAO24 hex via OpenSky. Anonymous works; OAuth2 client credentials unlock more. |
| `faa-registry` | Look up FAA aircraft registry records (US tail numbers) by N-number, owner, or make/model. |

`pyagent-skills uninstall <name>` removes an installed copy. Project-local
skills under `./.pyagent/skills/` always override an installed one of the
same name — drop a `SKILL.md` in there to customize a bundled skill for a
single project without touching the installed copy.
