# Plugins

Plugins extend pyagent at runtime — they can register tools, contribute
prompt sections, and observe or control the conversation loop. Unlike
skills (which are passive markdown), plugins are active code that runs
alongside the agent.

A plugin is a directory with `manifest.toml` + `plugin.py` (drop-in)
or an installed Python package declaring an entry point in the
`pyagent.plugins` group.

## Discovery order

Three-tier (later wins on name collision, same as skills):

1. **Bundled** — `pyagent/plugins/<name>/`. Filtered against
   `built_in_plugins_enabled` in `config.toml`.
2. **Entry-point installed** — `pip install pyagent-foo`.
3. **Drop-in** — `<config-dir>/plugins/<name>/` (user) or
   `./.pyagent/plugins/<name>/` (project).

`pyagent-plugins list` shows what's discovered, with override
warnings when a higher-tier plugin shadows a lower-tier one.
`pyagent-plugins reset <name>` wipes a plugin's
`<config-dir>/plugins/<name>/` data dir.

## Bundled plugins

| Plugin | What it provides | Default enabled? |
| --- | --- | --- |
| `memory` | Markdown ledger storage + semantic recall — `add_memory`, `read_memory`, `write_memory`, `write_user`, `set_memory_description`, `recall_memory`, plus USER/MEMORY prompt sections. Root-only (does not load in subagents). | yes |
| `html-tools` | `html_select` — CSS-select against saved HTML attachments. Role-only (allowlisted in the bundled `researcher` role). | yes |
| `code-mapper` | `map_code` / `probe_grammar` — tree-sitter symbol map for source files (Python in v1; multi-language ready). | yes |
| `web-search` | `web_search` — DuckDuckGo-backed list search; side-saves structured JSON. Role-only (allowlisted in the bundled `researcher` role). | yes |
| `reddit-search` | `reddit_search` — public reddit.com/search.json. Side-saves structured JSON. | yes |
| `hn-search` | `hn_search` — Algolia-backed Hacker News search. Side-saves structured JSON. | yes |
| `doc-tools` | `extract_doc` / `summarize_doc` — sub-LLM document tools. | no (opt-in; pick a model first) |
| `claude-code-cli` | `claude_code_cli` — pipe a prompt into Anthropic's `claude -p`. Self-disables when `claude` isn't on PATH. | yes |
| `ollama` | Registers `ollama` as an LLM provider. `pyagent --list-models` enumerates pulled models. | yes |
| `py-dev-toolkit` | `lint` / `typecheck` / `run_pytest` for Python projects. | yes |
| `echo-plugin` | Test/demo provider that echoes the most recent user message. Exercises the plugin → llm-router wiring without spending tokens. | yes |

To remove a plugin from the catalog, set `built_in_plugins_enabled`
in `config.toml` to the list of names you want kept. An empty list
disables every bundled plugin.

## Authoring a plugin

The full design and API surface live in
[docs/plugin-design.md](plugin-design.md) and
[docs/plugin-feature-summary.md](plugin-feature-summary.md). Quick
start:

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
`pyagent/plugins/memory/` for a complete bundled example exercising
tools, prompt sections, and lifecycle hooks.

The bundled `write-plugin` skill (enabled by default) walks the agent
through writing a plugin for you — load it with `read_skill("write-plugin")`.
