"""Local stub LLM clients for pyagent itself.

Useful for testing the harness end-to-end without hitting an external
API: the system prompt assembly, tool schema generation, session
persistence, gutter UI, and skill discovery all exercise without
spending real tokens. Selected with `--model pyagent/<stub-name>`.
"""

import random
from typing import Any, Callable


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
        self.provider_model = f"pyagent/{model}"

    # Stub clients have no real context budget; reporting 0 makes the
    # CLI's context-warning machinery treat them as "window unknown"
    # and skip the footer segment entirely. They're for harness/UX
    # testing where the budget question doesn't apply.
    context_window: int = 0

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        text = ""
        for msg in reversed(conversation):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                text = content
                break
        # Word-by-word streaming so the protocol exercise covers the
        # multi-delta path even on the simplest stub. Fires
        # synchronously — no sleep — so tests stay fast.
        if on_text_delta and text:
            for i, word in enumerate(text.split(" ")):
                chunk = word if i == 0 else f" {word}"
                on_text_delta(chunk)
        return {
            "text": text,
            "tool_calls": [],
            "usage": {
                "input": 0,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "model": self.provider_model,
            },
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
        self.provider_model = f"pyagent/{model}"

    context_window: int = 0

    def respond(
        self,
        conversation: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        system_volatile: str | None = None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        paragraphs = []
        for _ in range(random.randint(1, 5)):
            sentences = random.choices(
                _LOREM_SENTENCES, k=random.randint(2, 6)
            )
            paragraphs.append(" ".join(sentences))
        text = "\n\n".join(paragraphs)
        # Sentence-by-sentence streaming gives a realistic pacing
        # cadence when used with a CLI renderer (paragraphs flow in
        # noticeable chunks, mid-sentence lag is rare). No sleeps —
        # the consumer drives any pacing it wants.
        if on_text_delta:
            buf: list[str] = []
            remaining = text
            for sent in _split_for_stream(text):
                buf.append(sent)
                on_text_delta(sent)
                remaining = remaining[len(sent):]
        return {
            "text": text,
            "tool_calls": [],
            "usage": {
                "input": 0,
                "output": 0,
                "cache_creation": 0,
                "cache_read": 0,
                "model": self.provider_model,
            },
        }


def _split_for_stream(text: str) -> list[str]:
    """Break the lorem text into bite-sized streaming chunks.

    Splits on sentence-ending punctuation while keeping the trailing
    delimiter + any whitespace attached to the chunk that ends with
    it, so concatenating the chunks reproduces the input exactly.
    Used only by `LoremClient` — real providers' streaming chunks
    come pre-segmented from the wire.
    """
    chunks: list[str] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in ".!?\n":
            # consume trailing whitespace into this chunk so the next
            # one starts cleanly with a non-space character
            end = i + 1
            while end < len(text) and text[end] in " \t":
                end += 1
            chunks.append(text[start:end])
            start = end
    if start < len(text):
        chunks.append(text[start:])
    return chunks
