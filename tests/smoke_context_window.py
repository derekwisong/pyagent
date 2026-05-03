"""End-to-end smoke for the per-model context-window awareness.

Concerns:

  1. **Per-client context_window lookup.** Each built-in client
     reports a non-zero, model-specific window from a hardcoded
     table. Stub clients (pyagent/echo, pyagent/loremipsum) report
     0 so the CLI machinery hides the segment for them.
  2. **Ollama lazy /api/show fetch + cache.** First read calls the
     server; subsequent reads return the cached value without a
     second HTTP call. Lookup failure (server down, model gone,
     missing field) caches 0 — no retry storm.
  3. **`agent_proc._emit_context_status` event shape.** With a real
     usage figure and a known window, fires `context_status` with
     `pct/used/window`. With window=0 (stub clients), emits
     nothing. With used=0 (very first turn before any input
     counted), also emits nothing — protects against a bogus 0%
     reading on session start.
  4. **Threshold-crossing info events.** Crossing the 60/80/95
     boundary emits one `info` per crossing; staying above doesn't
     re-emit; a single jump that vaults multiple tiers (e.g. 50% →
     90%) emits the highest-tier info, not a chain of three.
  5. **CLI footer segment.** `_context_segment` returns the right
     string at 0/50/85/96%, with yellow/red colorization at
     threshold and empty when window=0.

Run with:

    .venv/bin/python -m tests.smoke_context_window
"""

from __future__ import annotations

import os
from unittest import mock

from pyagent.cli import _context_segment


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_builtin_context_windows() -> None:
    """Each built-in client reports a positive, model-specific
    window via the new `context_window` property. We don't
    construct the SDK clients (no API keys); we exercise the
    static table directly."""
    from pyagent.llms import anthropic as anthropic_mod
    from pyagent.llms import gemini as gemini_mod
    from pyagent.llms import openai as openai_mod

    _check(
        "anthropic table contains the catalog model",
        anthropic_mod._CONTEXT_WINDOWS["claude-sonnet-4-6"] == 200_000,
        repr(anthropic_mod._CONTEXT_WINDOWS),
    )
    _check(
        "anthropic default for unknown name is positive",
        anthropic_mod._DEFAULT_CONTEXT_WINDOW > 0,
    )

    _check(
        "openai distinguishes o-series from chat models",
        openai_mod._CONTEXT_WINDOWS["o1"] > openai_mod._CONTEXT_WINDOWS["gpt-4o"],
        repr(openai_mod._CONTEXT_WINDOWS),
    )

    _check(
        "gemini 2.5 family is 2M",
        gemini_mod._CONTEXT_WINDOWS["gemini-2.5-flash"] == 2_000_000,
    )
    _check(
        "gemini 2.0 family is 1M (older ceiling)",
        gemini_mod._CONTEXT_WINDOWS["gemini-2.0-flash"] == 1_000_000,
    )


def _check_pyagent_stub_context_window_is_zero() -> None:
    """Stub clients have no real budget; reporting 0 hides the
    footer segment."""
    from pyagent.llms.pyagent import EchoClient, LoremClient

    _check(
        "EchoClient.context_window == 0",
        EchoClient().context_window == 0,
    )
    _check(
        "LoremClient.context_window == 0",
        LoremClient().context_window == 0,
    )


def _check_ollama_lazy_show_fetch_and_cache() -> None:
    """Ollama queries `/api/show` on first read of `context_window`,
    caches the result, and returns 0 on lookup failure without
    retrying."""
    from pyagent.plugins.ollama import client as ollama_client_mod

    class _OK:
        ok = True
        status_code = 200

        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

        def raise_for_status(self):
            pass

    calls = []

    def fake_post(url, json=None, timeout=None, stream=False):
        calls.append(json)
        return _OK(
            {
                "model_info": {
                    # Architecture-prefixed key matches the live
                    # Ollama `/api/show` response shape.
                    "llama.context_length": 131072,
                    "general.architecture": "llama",
                }
            }
        )

    c = ollama_client_mod.OllamaClient(model="llama3.2")
    with mock.patch.object(
        ollama_client_mod.requests, "post", side_effect=fake_post
    ):
        first = c.context_window
        second = c.context_window
        third = c.context_window

    _check(
        "first read parsed context_length from model_info",
        first == 131072,
        repr(first),
    )
    _check(
        "subsequent reads hit the cache (no extra HTTP)",
        len(calls) == 1,
        f"calls={len(calls)}",
    )
    _check(
        "cached value is stable across reads",
        first == second == third,
    )

    # Failure path: connection error → caches 0, doesn't retry.
    def boom(*a, **kw):
        raise ConnectionError("server down")

    c2 = ollama_client_mod.OllamaClient(model="x")
    boom_calls: list = []

    def fake_post_boom(*a, **kw):
        boom_calls.append(1)
        raise ConnectionError("server down")

    with mock.patch.object(
        ollama_client_mod.requests, "post", side_effect=fake_post_boom
    ):
        v1 = c2.context_window
        v2 = c2.context_window
    _check(
        "lookup failure caches 0",
        v1 == 0 and v2 == 0,
        f"v1={v1} v2={v2}",
    )
    _check(
        "lookup failure doesn't retry on subsequent reads",
        len(boom_calls) == 1,
        f"calls={len(boom_calls)}",
    )

    # No model_info at all → caches 0 cleanly.
    def fake_post_empty(url, json=None, timeout=None, stream=False):
        return _OK({"capabilities": ["completion"]})

    c3 = ollama_client_mod.OllamaClient(model="z")
    with mock.patch.object(
        ollama_client_mod.requests, "post", side_effect=fake_post_empty
    ):
        _check(
            "missing model_info → context_window=0",
            c3.context_window == 0,
        )


def _check_emit_context_status_shape() -> None:
    """`_emit_context_status` fires the right events for the right
    inputs, and skips emission when there's nothing useful to say."""
    from pyagent.agent_proc import _emit_context_status

    class _Client:
        def __init__(self, window):
            self.context_window = window

    class _Agent:
        def __init__(self, client, used):
            self.client = client
            self.token_usage = {"input": used}

    class _State:
        def __init__(self):
            self.events: list[tuple] = []
            self._context_warn_tier = -1

        def send(self, event_type, **payload):
            self.events.append((event_type, payload))

    # Healthy reading: 50% utilization → context_status only, no
    # info (we're under the 60% tier).
    state = _State()
    _emit_context_status(state, _Agent(_Client(200_000), used=100_000))
    _check(
        "50% utilization emits context_status only",
        state.events == [
            ("context_status", {"pct": 50, "used": 100_000, "window": 200_000})
        ],
        repr(state.events),
    )

    # Crossing 60% from below → context_status + info(level=info).
    state = _State()
    _emit_context_status(state, _Agent(_Client(200_000), used=130_000))
    types = [e[0] for e in state.events]
    _check(
        "65% emits context_status + info",
        types == ["context_status", "info"],
        repr(types),
    )
    _check(
        "60% tier latched on _ChildState",
        state._context_warn_tier == 0,
    )

    # Staying above 60% on next call → no fresh info.
    _emit_context_status(state, _Agent(_Client(200_000), used=140_000))
    types = [e[0] for e in state.events]
    _check(
        "stay-above tier doesn't re-emit info",
        types == ["context_status", "info", "context_status"],
        repr(types),
    )

    # Multi-tier jump in one call (50% → 90%): info fires once at
    # the highest tier we crossed, not once per tier.
    state = _State()
    _emit_context_status(state, _Agent(_Client(200_000), used=180_000))
    types = [e[0] for e in state.events]
    _check(
        "multi-tier jump emits one info at highest tier",
        types == ["context_status", "info"],
        repr(types),
    )
    _check(
        "highest tier (80% bucket) latched, not 60%",
        state._context_warn_tier == 1,
        f"tier={state._context_warn_tier}",
    )

    # Window=0 (stub client): emit nothing.
    state = _State()
    _emit_context_status(state, _Agent(_Client(0), used=999))
    _check(
        "window=0 emits nothing (footer hides the segment)",
        state.events == [],
        repr(state.events),
    )

    # Used=0 (no LLM calls yet): emit nothing — avoids a bogus 0%
    # before the first turn finishes.
    state = _State()
    _emit_context_status(state, _Agent(_Client(200_000), used=0))
    _check(
        "used=0 emits nothing",
        state.events == [],
        repr(state.events),
    )


def _check_context_segment_renders() -> None:
    """`_context_segment` reads from agents['root']['context'] and
    formats it with threshold colors."""
    # No state → empty.
    _check(
        "no agents → empty",
        _context_segment({}) == "",
    )
    _check(
        "no context key → empty",
        _context_segment({"root": {"status": "thinking"}}) == "",
    )

    # window=0 → empty (stub clients).
    agents = {"root": {"context": {"pct": 0, "used": 0, "window": 0}}}
    _check(
        "window=0 → empty",
        _context_segment(agents) == "",
    )

    # Healthy 50% → plain ` · ctx: 50%`.
    agents = {"root": {"context": {"pct": 50, "used": 100_000, "window": 200_000}}}
    _check(
        "50% → plain segment, no color",
        _context_segment(agents) == " · ctx: 50%",
        repr(_context_segment(agents)),
    )

    # 85% → yellow.
    agents["root"]["context"]["pct"] = 85
    seg = _context_segment(agents)
    _check(
        "85% → yellow markup",
        "yellow" in seg and "85%" in seg,
        seg,
    )

    # 96% → red.
    agents["root"]["context"]["pct"] = 96
    seg = _context_segment(agents)
    _check(
        "96% → red markup",
        "red" in seg and "96%" in seg,
        seg,
    )


def main() -> None:
    _check_builtin_context_windows()
    _check_pyagent_stub_context_window_is_zero()
    _check_ollama_lazy_show_fetch_and_cache()
    _check_emit_context_status_shape()
    _check_context_segment_renders()
    print("smoke_context_window: all checks passed")


if __name__ == "__main__":
    main()
