"""pyagent — a tool-using LLM agent.

Two surfaces, same engine:

  - **CLI**: ``pyagent`` (see ``pyagent --help``). Sessions, plugins,
    skills, subagents, the full feature set.
  - **Library**: ``from pyagent import Agent, auto_client``. The bare
    Agent loop without sessions/plugins/permissions, suitable for
    embedding in a Python app, notebook, or service.

Library quickstart::

    from pyagent import Agent, auto_client

    def add(a: int, b: int) -> int:
        '''Add two integers.'''
        return a + b

    agent = Agent(
        client=auto_client(),
        system="You are a helpful calculator.",
    )
    agent.add_tool("add", add)
    print(agent.run("What is 17 + 25?"))

The function's type hints and docstring become the JSON schema sent
to the model. ``agent.run(prompt)`` returns the concatenated assistant
text after the tool-using loop terminates.

For longer-form library examples (sessions, custom permission
handlers, attaching the bundled plugins, streaming callbacks), see
``docs/library-usage.md``.
"""

from pyagent.agent import Agent
from pyagent.llms import (
    LLMClient,
    auto_detect_provider,
    get_client,
    resolve_model,
)
from pyagent.session import Attachment, Session


def auto_client() -> LLMClient:
    """Pick an LLMClient based on environment variables.

    Order: ``ANTHROPIC_API_KEY`` → ``OPENAI_API_KEY`` →
    ``GEMINI_API_KEY`` (or ``GOOGLE_API_KEY``). The first that's set
    wins and uses the provider's default model. Raises
    ``RuntimeError`` if no key is set.

    For explicit model selection, use::

        from pyagent import get_client
        client = get_client("anthropic/claude-opus-4-7")
        client = get_client("openai/gpt-4o-mini")
        client = get_client("ollama/llama3.2:latest")  # plugin provider

    Provider/model strings match the ``--model`` flag the CLI accepts.
    """
    spec = auto_detect_provider()
    if spec is None:
        raise RuntimeError(
            "no LLM API key found in environment. Set one of: "
            "ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY "
            "(or GOOGLE_API_KEY)."
        )
    return spec.factory()


__all__ = [
    "Agent",
    "Attachment",
    "LLMClient",
    "Session",
    "auto_client",
    "get_client",
    "resolve_model",
]
