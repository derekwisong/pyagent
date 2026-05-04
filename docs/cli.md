# Pyagent CLI

The terminal interface to pyagent. Sessions, model switching, queue
management, resets — everything you do at the prompt or via flags.

## Run the agent

After installing:

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
pyagent --model ollama/llama3.2:latest         # local, via the bundled ollama plugin
```

If `--model` is omitted, pyagent picks one in this order:

1. `default_model` in `config.toml` (e.g. `default_model = "openai"`).
2. Auto-detect from the API-key env vars: `ANTHROPIC_API_KEY` →
   `OPENAI_API_KEY` → `GEMINI_API_KEY` / `GOOGLE_API_KEY`. The first
   that's set wins.
3. If none are set, pyagent exits with a pointed error naming the
   env vars it looks for.

The session header prints the resolved provider/model so you can confirm
what was picked.

Ollama uses the local daemon at `http://localhost:11434` by default. Set
`OLLAMA_HOST` to point at a different host/port (e.g.,
`OLLAMA_HOST=http://192.168.1.10:11434`). `OLLAMA_MODEL` sets the default
model name when `--model ollama` is passed without a `/<name>` suffix.
Neither variable is required for the standard local setup.

### Switching models mid-session

At the prompt, type `/model <spec>` to swap the running agent's LLM
client without restarting:

```
> /model openai/gpt-4o
> /model anthropic
> /model planner          # role name, see Configuration → Roles
```

The swap takes effect on the next API call. The status footer updates
to reflect the new model. A bad spec leaves the existing client in
place and prints a warning. Subagents are not affected — each child
keeps the model it was spawned with.

## Talking to a busy agent

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

## Inspecting the system prompt

`pyagent --prompt-dump` renders the system prompt the agent would assemble
this turn and prints it to stdout. Useful for auditing what the model
actually sees, or previewing a candidate persona file change before
applying it. Add `--prompt-include-schemas` to also dump the JSON tool
schemas. `--soul` / `--tools` / `--primer` overrides preview alternate
persona files without applying them.

## Sessions

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

Resume an existing session with `pyagent --resume <session-id>`, or
`pyagent --resume` with no value to list them.

## Resetting persona files

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
`pyagent-plugins reset <name>` (e.g. `pyagent-plugins reset memory`
to clear USER and MEMORY ledgers).

## Companion CLIs

Pyagent ships several focused CLIs alongside the main `pyagent` command:

| Command | Purpose |
|---|---|
| `pyagent-config` | Inspect or initialize `config.toml`. |
| `pyagent-skills` | List, install, uninstall skills. |
| `pyagent-plugins` | List discovered plugins, reset plugin data. |
| `pyagent-sessions` | List, delete, prune saved sessions. |
| `pyagent-roles` | Manage role files for subagents. |
| `pyagent-bench` | Run benchmark scenarios. |

Each accepts `--help` for the full surface.
