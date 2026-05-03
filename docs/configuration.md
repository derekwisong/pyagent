# Configuration

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

## Common keys

```toml
default_model = "anthropic"

built_in_skills_enabled = ["write-skill", "write-plugin", "pdf-from-markdown"]
built_in_plugins_enabled = [
  "memory-markdown", "memory-vector", "html-tools",
  "web-search", "reddit-search", "hn-search",
  "code-mapper", "claude-code-cli", "ollama", "py-dev-toolkit",
]

[subagents]
max_depth = 3      # spawn-tree height; root is depth 0
max_fanout = 5     # simultaneous children any single agent can hold

[session]
attachment_dir_cap_mb = 25   # per-session attachments LRU cap
```

Plugin-specific config goes under `[plugins.<plugin-name>]` tables —
see each plugin's source for what it reads. Examples: `[plugins.doc-tools]`
for the sub-LLM doc tools, `[plugins.web-search]` for retry/backoff
tuning, `[plugins.reddit-search]` for User-Agent override.

## Roles (named subagent models)

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

`pyagent-roles list` shows defined roles. Bundled role files live in
`pyagent/roles_bundled/` and can be referenced by name; user-defined
roles in `config.toml` override or extend them.

## Where files live

| Tier | Path |
|---|---|
| User config | `~/.config/pyagent/` (Linux) / `~/Library/Application Support/pyagent/` (macOS) / `%APPDATA%\pyagent\` (Windows) |
| Project config | `./.pyagent/config.toml` |
| Persona files | `<config-dir>/SOUL.md`, `TOOLS.md`, `PRIMER.md` (overridable per-project by placing in cwd) |
| Plugin data | `<config-dir>/plugins/<name>/` (e.g. `memory-markdown/MEMORY.md`) |
| Sessions | `./.pyagent/sessions/<session-id>/` |

See [docs/cli.md](cli.md) for resetting any of these to bundled defaults.
