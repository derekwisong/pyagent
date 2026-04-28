"""Local stub LLM clients for pyagent itself.

Useful for testing the harness end-to-end without hitting an external
API: the system prompt assembly, tool schema generation, session
persistence, gutter UI, and skill discovery all exercise without
spending real tokens. Selected with `--model pyagent/<stub-name>`.
"""

import random
from typing import Any


class EchoClient:
    """Stub LLM whose `respond` returns the most recent user message
    verbatim and requests no tool calls.

    The agent loop terminates the moment a turn has no tool calls, so
    this client always produces a one-shot reply: user types a prompt,
    sees it echoed back, terminal returns to the input. Tool schemas
    and the system prompt are accepted (so the agent's wiring still
    runs) but ignored.

    Attributes:
        model: The name reported back to the CLI/header. Defaults to
            "echo"; reusable for other stub flavors if we add them.
    """

    def __init__(self, model: str = "echo") -> None:
        self.model = model

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = ""
        for msg in reversed(conversation):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
                break
        return {
            "text": text,
            "tool_calls": [],
            "usage": {"input": 0, "output": 0},
        }


_LOREM_SENTENCES = [
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.",
    "Duis aute irure dolor in reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur.",
    "Excepteur sint occaecat cupidatat non proident, sunt in culpa qui officia deserunt mollit anim id est laborum.",
    "Curabitur pretium tincidunt lacus, nulla gravida orci a odio.",
    "Nullam varius, turpis et commodo pharetra, est eros bibendum elit, nec luctus magna felis sollicitudin mauris.",
    "Integer in mauris eu nibh euismod gravida.",
    "Duis ac tellus et risus vulputate vehicula.",
    "Donec lobortis risus a elit. Etiam tempor.",
    "Ut ullamcorper, ligula eu tempor congue, eros est euismod turpis, id tincidunt sapien risus a quam.",
    "Maecenas fermentum consequat mi.",
    "Donec fermentum. Pellentesque malesuada nulla a mi.",
    "Duis sapien sem, aliquet nec, commodo eget, consequat quis, neque.",
    "Aliquam faucibus, elit ut dictum aliquet, felis nisl adipiscing sapien, sed malesuada diam lacus eget erat.",
    "Cras mollis scelerisque nunc. Nullam arcu.",
    "Aliquam consequat. Curabitur augue lorem, dapibus quis, laoreet et, pretium ac, nisi.",
    "Aenean magna nisl, mollis quis, molestie eu, feugiat in, orci.",
]


class LoremClient:
    """Stub LLM that emits randomly-lengthed lorem ipsum text.

    Like `EchoClient` it returns no tool calls, so the agent loop
    terminates after one turn. Each response is between 1 and 5
    paragraphs, each paragraph 2 to 6 sentences sampled (with
    replacement) from a small corpus. Useful for stress-testing the
    gutter renderer, paragraph spacing, and scrolling without burning
    real tokens.
    """

    def __init__(self, model: str = "loremipsum") -> None:
        self.model = model

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        paragraphs = []
        for _ in range(random.randint(1, 5)):
            sentences = random.choices(
                _LOREM_SENTENCES, k=random.randint(2, 6)
            )
            paragraphs.append(" ".join(sentences))
        return {
            "text": "\n\n".join(paragraphs),
            "tool_calls": [],
            "usage": {"input": 0, "output": 0},
        }
