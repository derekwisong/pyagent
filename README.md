# Pyagent

A multi-provider agent framework with plugin, skill, and subagent systems.
Designed to explore how agent loops, tool calling, memory, and orchestration
can be structured outside of large frameworks like LangChain.

## Features

- **Chat**: `pyagent` for a chat that can read files, run shell
  commands, search the web, and call any tools you've added.
- **Plugins, skills, memory**: opt into what you need.
- **Subagents**: Subagents work independently and can communicate with their parent.
- **Configurable**: Pyagent can be configured at the workspace and user level.
- **Library**: `from pyagent import Agent, auto_client` to embed the
  agent loop in your own app, notebook, or service.
- **Bring your own model**: Anthropic, OpenAI, Gemini, or local Ollama.

[![asciicast](https://asciinema.org/a/5FvXNO6wzrSkKVwd.svg)](https://asciinema.org/a/5FvXNO6wzrSkKVwd)

## Quick start

```bash
pip install git+https://github.com/derekwisong/pyagent.git
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
schemas.

## What's inside

At first, this was a simple `Agent` class, written by hand as an exercise to learn
how to build a flexible tool-calling agent.

I used Claude Code to expand and build more of the features needed for
a complete agent.

| | |
|---|---|
| **System Prompt Builder** | Build the system prompt through [SOUL.md](pyagent/defaults/SOUL.md) [PRIMER.md](pyagent/defaults/PRIMER.md) and more; leverages provider caching |
| **Tool calling** | Plain Python functions become tools — type hints + docstrings drive the schema. |
| **Sessions** | Resumable conversations. |
| **Plugins** | Write [plugins](docs/plugins.md) in Python to provide tools and hooks to extend Pyagent |
| **Skills** | [Skills](docs/skills.md) are lazy-loaded "how do I" docs the agent reads on demand. |
| **Subagents** | Spawn focused child agents; bidirectional comms (`ask_parent`, `tell_subagent`, async fan-out). |
| **Memory** | USER ledger + MEMORY index, with semantic vector search recall via fastembed. Auto-loaded into the prompt. |
| **Multi-model** | Switch mid-session with `/model`, define named roles per subagent in `config.toml`. |
| **CLI** | Basic CLI REPL to converse with the agent |

## Environment variables

| Variable | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `--model anthropic` | |
| `OPENAI_API_KEY` | `--model openai` | |
| `GEMINI_API_KEY` | `--model gemini` | `GOOGLE_API_KEY` is accepted as a fallback. |
| `OLLAMA_HOST` | `--model ollama/...` | Optional. Default `http://localhost:11434`. Set when Ollama runs on a non-default host or port. |
| `OLLAMA_MODEL` | `--model ollama` | Optional. Default model when no `/<name>` suffix is given. |

Local Ollama needs no key — just the daemon running and a model pulled.

## License

MIT — see [LICENSE](LICENSE).
