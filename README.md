# Pyagent

A small, hackable, tool-using LLM agent in Python.

- **Terminal**: `pyagent` for a chat that can read files, run shell
  commands, search the web, and call any tools you've added.
- **Library**: `from pyagent import Agent, auto_client` to embed the
  agent loop in your own app, notebook, or service.
- **Bring your own model**: Anthropic, OpenAI, Gemini, or local Ollama.
- **Plugins, skills, subagents, memory**: opt into what you need.

## Quick start

```bash
pip install -e .                    # editable install from a clone
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY / GEMINI_API_KEY
pyagent                             # start chatting
```

Requires Python 3.11+. `pyagent --help` lists every flag; common ones
like `--model`, `--resume`, and `--prompt-dump` are documented in
[docs/cli.md](docs/cli.md).

## As a library

```python
from pyagent import Agent, get_client

def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

client = get_client("ollama/llama3.2:latest")   # local, no API key
# client = get_client("anthropic/claude-sonnet-4-6")
# client = get_client("openai/gpt-4o-mini")

agent = Agent(client=client, system="You are a helpful calculator.")
agent.add_tool("add", add)
print(agent.run("What is 17 + 25?"))
```

Type hints and docstrings become the tool schema — no hand-written
schemas. The bare-Agent path loads no plugins, skills, or built-in
file/shell tools; you opt in. See
[docs/library-usage.md](docs/library-usage.md) for sessions, streaming
callbacks, the permissions gate, and a la carte feature attach.

## What's inside

| | |
|---|---|
| **Tool calling** | Plain Python functions become tools — type hints + docstrings drive the schema. |
| **Sessions** | Conversations persist across runs; large tool outputs auto-offload to disk. |
| **Plugins** | Bundled `web_search`, `reddit_search`, `hn_search`, `doc_tools`, `code_mapper`, memory ledger, and more. Extensible at runtime. |
| **Skills** | Lazy-loaded "how do I" docs the agent reads on demand (`pdf-from-markdown`, `aviation-weather`, ...). |
| **Subagents** | Spawn focused child agents; bidirectional comms (`ask_parent`, `tell_subagent`, async fan-out). |
| **Memory** | USER ledger + MEMORY index, with semantic recall via fastembed. Auto-loaded into the prompt. |
| **Multi-model** | Switch mid-session with `/model`, define named roles per subagent in `config.toml`. |

## Documentation

- [**docs/cli.md**](docs/cli.md) — running pyagent, model selection, sessions, resets, slash commands.
- [**docs/library-usage.md**](docs/library-usage.md) — using pyagent from Python.
- [**docs/configuration.md**](docs/configuration.md) — `config.toml`, defaults, named subagent roles.
- [**docs/skills.md**](docs/skills.md) — bundled skills, layout, authoring.
- [**docs/plugins.md**](docs/plugins.md) — bundled plugins, discovery order, brief authoring quickstart.
- [**docs/design.md**](docs/design.md) — how the loop, tools, system prompt, memory, and subagents fit together.
- [**docs/plugin-design.md**](docs/plugin-design.md) — full plugin authoring API.

## Environment variables

| Variable | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `--model anthropic` | |
| `OPENAI_API_KEY` | `--model openai` | |
| `GEMINI_API_KEY` | `--model gemini` | `GOOGLE_API_KEY` is accepted as a fallback. |

Local Ollama needs no key — just the daemon running and a model pulled.

## License

Copyright © 2026 Derek Wisong. All rights reserved. No license is
granted for use, modification, or redistribution at this time.
