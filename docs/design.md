# Pyagent design

How the agent loop is wired, where tools come from, how the system
prompt is assembled, how memory works.

## The agent loop

The loop lives in `Agent.run()` and does the same thing every turn:

- Append the user's prompt to the conversation.
- Send the conversation, the system prompt, and the tool schemas to the model.
- Append the assistant's reply to the conversation.
- If the reply has no tool calls, return its text — turn over.
- Otherwise, run each requested tool, append the results as a `tool_results`
  message, and loop back to the model.

The model decides when it's done by simply not asking for any more tools.

## Tools

Tools give the agent hands. The LLM requests tool calls, the agent
invokes them and returns the results.

> A tool is simply a Python function that returns a `str`.

Built-in tools shipped with pyagent:

| Tool | What it does |
| --- | --- |
| `read_file` | Read a text file, optionally a line range. Auto-truncates above 2000 lines. |
| `write_file` | Write content to a file, overwriting any existing one. |
| `edit_file` | Exact-match diff edit; only sends the diff. |
| `list_directory` | List the entries in a directory; directories are suffixed `/`. |
| `grep` | Regex pattern search with optional `before` / `after` / `context` lines. |
| `glob` | Recursive name match (`**/*.py` style). |
| `execute` | Run a shell command (60s timeout). A small regex blocklist refuses obviously dangerous patterns. |
| `run_background` / `read_output` / `wait_for` / `kill_process` | Long-running shell quartet. |
| `fetch_url` | HTTP GET. Always saves the raw body to a session attachment; by default also returns markdown of the article body inline. |
| `list_plugins` | List the plugins currently loaded. Self-improvement helper. |

HTML tools (`html_to_md` / `html_select`) come from the bundled
`html-tools` plugin and operate on saved attachments (or any local
HTML file). Memory tools (`read_ledger`, `write_ledger`, `add_memory`)
come from the `memory-markdown` plugin; semantic recall (`recall_memory`)
from `memory-vector`. Other bundled plugins (`code-mapper`, `web-search`,
`reddit-search`, `hn-search`, `claude-code-cli`, `doc-tools`) register
additional tools — see [docs/plugins.md](plugins.md) for the full list.

File tools resolve paths and refuse anything outside the workspace unless the
human approves at a prompt. See `pyagent/permissions.py`. The user's pyagent
config dir is pre-approved so the agent can read/write its persona and
plugin data without prompting.

## SOUL, TOOLS, PRIMER — the system prompt

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
Windows: `%APPDATA%\pyagent\`). On first run, the bundled defaults from
the package are copied in; after that, edits to the config-dir copies
take effect immediately. A file with the same name in the current
working directory takes precedence — handy for per-project SOUL/TOOLS
overrides or for hacking on the prompts inside the pyagent repo itself.

Paths can be overridden with `--soul`, `--tools`, `--primer`. To dump
the rendered prompt for inspection, use `pyagent --prompt-dump` (see
[docs/cli.md](cli.md)).

### Cache breakpoint

The system prompt has a stable segment (cached prefix) and a volatile
segment (after the cache breakpoint). Plugin-contributed sections can
opt into either; sections likely to change turn-to-turn (e.g. live
state) go in volatile so they don't invalidate the cached prefix.

## Memory

Long-term memory is provided by two bundled plugins:

- **`memory-markdown`** is the storage backend. Tools: `read_ledger`,
  `write_ledger`, `add_memory`. Backed by markdown files under
  `<config-dir>/plugins/memory-markdown/` — a single `USER.md` (always
  splatted into the system prompt), a `MEMORY.md` index (also auto-loaded),
  and a `memories/` directory of individual entries (loaded on demand).
  `add_memory` writes the body and updates the index in one atomic call.
- **`memory-vector`** layers semantic recall on top via `recall_memory`,
  indexing the same files with fastembed (BGE-small-en-v1.5). The two
  plugins are loosely coupled — `memory-vector` reads from
  `memory-markdown`'s data dir at runtime; if `memory-markdown` is
  disabled, `recall_memory` just reports that nothing is indexed.

`memory-markdown` also contributes prompt sections that auto-load
USER content and the MEMORY index into every system prompt. Drop both
plugins from `built_in_plugins_enabled` in `config.toml` to remove
memory entirely — the agent will have no memory tools and no memory
prose.

Memory work is meant to happen **organically**, mid-conversation: when
the agent learns a preference, a convention, or something genuinely
worth remembering, it updates the appropriate ledger then and there.

To wipe memory data: `pyagent-plugins reset memory-markdown`.

## Sessions and attachments

Conversation history lives under `./.pyagent/sessions/<session-id>/`.
Each session has a `conversation.jsonl` and an `attachments/` directory.
Tool results that exceed the inline threshold are written to
`attachments/` and replaced in-conversation with a short reference
(see `pyagent/agent.py:_render_tool_result`). The reference includes
size + range metadata so the agent can size follow-up reads correctly.

Per-session attachment-dir size is bounded by an LRU cap (default 25 MB,
configurable via `[session] attachment_dir_cap_mb`). When a write pushes
the dir over the cap, oldest-atime files are evicted until back under.
The just-written file is exempt from eviction.

## Subagents

Pyagent supports nested agents — one running agent can spawn focused
child agents to do isolated work. The mechanism is `multiprocessing.spawn`
plus a duplex pipe carrying the event protocol in `pyagent/protocol.py`.

Why subagents:

- **Fan-out**: independent jobs handled in parallel, results gathered.
- **Context insulation**: open-ended research / log spelunking happens in
  the child's window, only the conclusion comes back.
- **Fresh eyes**: review or critique by an agent with a different system
  prompt, getting perspective the parent can't have.

Subagent communication is bidirectional: parent ↔ child via
`call_subagent` (sync), `call_subagent_async` + `wait_for_subagents`
(parallel gather), `ask_parent` (child asks parent a question mid-task),
`tell_subagent` / `peek_subagent` (parent pushes notes / inspects child
state). See `pyagent/agent_proc.py` and `pyagent/subagent.py`.

Spawn caps live in `config.toml` under `[subagents]` (`max_depth`,
`max_fanout`) to prevent fork-bomb behavior on confused turns.

## Plugin system

Plugins are runtime-loaded extensions. They can register tools, contribute
prompt sections, observe or control the conversation loop, and register
LLM providers. See [docs/plugins.md](plugins.md) for the user-facing list
of bundled plugins, and `docs/plugin-design.md` for the author-facing
API.
