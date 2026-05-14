"""Minimal pyagent library example.

Run after `pip install -e .` from the repo root:

    ANTHROPIC_API_KEY=... python examples/quickstart.py

`auto_client()` picks a provider from the first env-var key it
finds (ANTHROPIC_API_KEY → OPENAI_API_KEY → GEMINI_API_KEY). To pin
a specific model, swap in `get_client("provider/model")`.
"""

from pyagent import Agent, auto_client


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def main() -> None:
    agent = Agent(
        client=auto_client(),
        system="You are a helpful calculator.",
    )
    agent.add_tool("add", add)
    print(agent.run("What is 17 + 25?"))


if __name__ == "__main__":
    main()
