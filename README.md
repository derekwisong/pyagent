# Pyagent

A multi-provider agent framework with plugin, skill, subagent, and
memory systems. Designed to explore how agent loops, tool calling,
and orchestration can be structured outside of large frameworks.

Use it two ways: `pyagent` for a terminal chat that can read files,
run shell commands, search the web, and call any tool you've added,
or `from pyagent import Agent, auto_client` to embed the same loop
in your own app, notebook, or service. Bring your own model —
Anthropic, OpenAI, Gemini, or local Ollama.

[![asciicast](https://asciinema.org/a/5FvXNO6wzrSkKVwd.svg)](https://asciinema.org/a/5FvXNO6wzrSkKVwd)

## Quick start

Not on PyPI yet — install from GitHub:

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
from pyagent import Agent, auto_client

def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

agent = Agent(client=auto_client(), system="You are a helpful calculator.")
agent.add_tool("add", add)
print(agent.run("What is 17 + 25?"))
```

`auto_client()` picks a provider from the first env-var key it finds
(`ANTHROPIC_API_KEY` → `OPENAI_API_KEY` → `GEMINI_API_KEY`). To pin a
specific model, use `get_client("anthropic/claude-sonnet-4-6")` or
`get_client("ollama/llama3.2:latest")` instead.

Type hints and docstrings become the tool schema — no hand-written
schemas. A runnable version of this snippet lives at
[examples/quickstart.py](examples/quickstart.py).

## What's inside

The interesting design choices: tools are plain Python functions
(no schema authoring), the system prompt is split into stable +
volatile halves to keep provider caches warm across turns, and the
plugin / skill / subagent boundary keeps each piece swappable. No
LangChain, no agent-DSL — just an explicit `Agent.run()` loop that
you can read top to bottom. See [docs/architecture.md](docs/architecture.md)
for diagrams.

| Feature | What it does |
|---|---|
| **System prompt builder** | Builds the prompt from [SOUL.md](pyagent/defaults/SOUL.md), [PRIMER.md](pyagent/defaults/PRIMER.md), and TOOLS sections; stable / volatile split keeps provider caches warm. |
| **Tool calling** | Plain Python functions become tools — type hints + docstrings drive the schema. |
| **Sessions** | Resumable conversations with on-disk JSONL transcripts. |
| **Plugins** | [Plugins](docs/plugins.md) register tools, prompt sections, and lifecycle hooks. |
| **Skills** | [Skills](docs/skills.md) are lazy-loaded "how do I" docs the agent reads on demand. |
| **Subagents** | Spawn focused child agents; bidirectional comms (`ask_parent`, `tell_subagent`) and parallel calls (`call_subagent_async` + `wait_for_subagents`). |
| **Memory** | USER ledger + [MEMORY](docs/design.md#memory) index with semantic recall via fastembed. Auto-loaded into the prompt. |
| **Multi-model** | Switch mid-session with `/model`; define named roles per subagent in `config.toml`. |
| **CLI** | Rich-rendered REPL with input queue, status footer, and slash commands. |

## Environment variables

| Variable | Used by | Notes |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | `--model anthropic` | |
| `OPENAI_API_KEY` | `--model openai` | |
| `GEMINI_API_KEY` | `--model gemini` | `GOOGLE_API_KEY` is accepted as a fallback. |
| `OLLAMA_HOST` | `--model ollama/...` | Optional. Default `http://localhost:11434`. Set when Ollama runs on a non-default host or port. |
| `OLLAMA_MODEL` | `--model ollama` | Optional. Default model when no `/<name>` suffix is given. |

Local Ollama needs no key — just the daemon running and a model pulled.

## Tests

Smoke tests live under `tests/` as standalone scripts (no pytest):

```bash
pip install -e '.[dev]'
pre-commit install                       # one-time: run black + ruff on every commit
python -m tests.test_token_meter        # run one
for f in tests/test_*.py; do python -m tests.$(basename "$f" .py); done
```

Three recall sub-tests in `test_plugins.py` are gated on
`PYAGENT_HEAVY_TESTS=1` because they download a ~130MB embedding
model. CI runs the same loop on every push and pull request.

## License

MIT — see [LICENSE](LICENSE).
