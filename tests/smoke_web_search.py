"""End-to-end smoke for the web-search plugin.

Concerns covered:

  1. **Plugin loads under the default config.** With "web-search" in
     `built_in_plugins_enabled`, `discover()` and `load()` produce
     both tools (`web_search`, `web_search_instant`).
  2. **Search formatter renders structured results as markdown.**
     `format_search_results` over a fixture list yields the
     numbered-list shape the agent sees, with title/url/snippet
     ordering preserved.
  3. **Instant-answer formatter picks the right field.** Answer
     beats abstract beats definition; missing data yields the
     `<no instant answer ...>` marker; `related=True` appends
     related-topic links.
  4. **Tool wrappers translate exceptions and return string shape.**
     Both tools must return `str` on success and on every error path,
     never raise into the agent loop.
  5. **Retry / backoff classifies failures.** Transient
     `DDGSException` / `TimeoutException` get retried with the
     configured backoff (default ``[1.0, 3.0]``); `RatelimitException`
     short-circuits with no retry; non-network exceptions skip the
     retry loop entirely and hit the existing catch-all marker.
  6. **Distinct error markers** — `<search error: rate limited; ...>`
     vs. `<search error: backend unavailable after N attempts; ...>`
     vs. the generic `<search error: ...>` so the agent can branch.
  7. **Configuration flows through.** ``retry_attempts``,
     ``retry_backoff_s``, and ``backend`` reach the DDGS call site;
     bogus values produce register-time warnings but tools still
     register and fall back to defaults.

Run with:

    .venv/bin/python -m tests.smoke_web_search
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from pyagent import config as config_mod, paths as paths_mod, plugins
from pyagent.plugins.web_search import register as web_search_register
from pyagent.plugins.web_search import search as web_search_mod


def _make_fake_api(plugin_config: dict | None = None) -> dict:
    """Build a fake plugin API and register the web-search plugin
    against it. Returns ``{"tools": {...}, "logs": [...], "plugin_config": ...}``.

    The captured ``logs`` list lets register-time-warning checks
    assert what the plugin emitted.
    """
    captured: dict = {
        "tools": {},
        "logs": [],
        "plugin_config": plugin_config if plugin_config is not None else {},
    }

    class _FakeAPI:
        @property
        def plugin_config(self):
            return captured["plugin_config"]

        def register_tool(self, name, fn):
            captured["tools"][name] = fn

        def log(self, level, message):
            captured["logs"].append((level, message))

    web_search_register(_FakeAPI())
    return captured


_FIXTURE_RESULTS_RAW = [
    {
        "title": "Best Python HTTP libraries 2025",
        "href": "https://example.com/python-http",
        "body": "A roundup of requests, httpx, and aiohttp for typical workloads.",
    },
    {
        "title": "httpx documentation",
        "href": "https://www.python-httpx.org/",
        "body": "HTTPX is a fully featured HTTP client for Python 3.",
    },
    {
        "title": "(no title)",
        "href": "https://example.com/missing",
        "body": "",
    },
]

# A representative DDG instant-answer payload — narrow but enough to
# exercise the parser and formatter. Real responses are larger.
_FIXTURE_INSTANT_PAYLOAD = {
    "Heading": "Python (programming language)",
    "AbstractText": (
        "Python is a high-level, general-purpose programming language."
    ),
    "AbstractSource": "Wikipedia",
    "AbstractURL": "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "Answer": "",
    "AnswerType": "",
    "Definition": "",
    "DefinitionSource": "",
    "DefinitionURL": "",
    "RelatedTopics": [
        {
            "Text": "CPython - the reference implementation",
            "FirstURL": "https://duckduckgo.com/CPython",
        },
        {
            "Name": "By type",
            "Topics": [
                {
                    "Text": "Pythonista (mobile IDE)",
                    "FirstURL": "https://duckduckgo.com/Pythonista",
                }
            ],
        },
    ],
}


def _check_search_formatter() -> None:
    """`format_search_results` produces the numbered-list shape."""
    results = [
        web_search_mod.SearchResult(
            title=r["title"], url=r["href"], snippet=r["body"]
        )
        for r in _FIXTURE_RESULTS_RAW
    ]
    md = web_search_mod.format_search_results(results, "python http")
    assert "# Search results for 'python http'" in md, md
    # Numbering preserved.
    assert "1. **Best Python HTTP libraries 2025**" in md, md
    assert "2. **httpx documentation**" in md, md
    assert "3. **(no title)**" in md, md
    # Snippets carried through (where present).
    assert "requests, httpx, and aiohttp" in md, md
    # URLs survive on the heading line.
    assert "https://example.com/python-http" in md, md
    print("✓ format_search_results renders numbered markdown list")


def _check_search_formatter_empty() -> None:
    md = web_search_mod.format_search_results([], "no hits")
    assert md.startswith("<no results"), md
    assert "no hits" in md, md
    print("✓ format_search_results: empty list → <no results ...> marker")


def _check_instant_parser_picks_abstract() -> None:
    ans = web_search_mod._parse_instant_payload(_FIXTURE_INSTANT_PAYLOAD)
    assert ans.heading == "Python (programming language)", ans
    assert "high-level" in ans.abstract, ans
    assert ans.abstract_source == "Wikipedia", ans
    # Related topics flattened across plain entries and grouped Topics.
    related_texts = [t for t, _ in ans.related]
    assert any("CPython" in t for t in related_texts), related_texts
    assert any("Pythonista" in t for t in related_texts), related_texts
    print("✓ _parse_instant_payload extracts abstract + flattens related")


def _check_instant_formatter_default() -> None:
    ans = web_search_mod._parse_instant_payload(_FIXTURE_INSTANT_PAYLOAD)
    out = web_search_mod.format_instant_answer(ans, "python", related=False)
    assert "Python (programming language)" in out, out
    assert "high-level" in out, out
    assert "Wikipedia" in out, out
    # related=False: no related-topic block.
    assert "Related:" not in out, out
    print("✓ format_instant_answer(default) emits abstract + source")


def _check_instant_formatter_with_related() -> None:
    ans = web_search_mod._parse_instant_payload(_FIXTURE_INSTANT_PAYLOAD)
    out = web_search_mod.format_instant_answer(ans, "python", related=True)
    assert "Related:" in out, out
    assert "CPython" in out, out
    assert "Pythonista" in out, out
    print("✓ format_instant_answer(related=True) appends related links")


def _check_instant_formatter_answer_beats_abstract() -> None:
    ans = web_search_mod.InstantAnswer(
        answer="42",
        abstract="The Hitchhiker's Guide to the Galaxy",
        abstract_source="Wikipedia",
    )
    out = web_search_mod.format_instant_answer(ans, "meaning of life")
    assert out.strip() == "42", out
    print("✓ format_instant_answer prefers Answer over Abstract")


def _check_instant_formatter_no_data() -> None:
    ans = web_search_mod.InstantAnswer()
    out = web_search_mod.format_instant_answer(ans, "obscure query")
    assert out.startswith("<no instant answer"), out
    assert "obscure query" in out, out
    print("✓ format_instant_answer: empty → <no instant answer ...> marker")


def _check_plugin_loads_under_default_config() -> None:
    """With the default config, web-search is in
    built_in_plugins_enabled and load() exposes both tools."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-websearch-"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
            assert "web-search" in cfg["built_in_plugins_enabled"], (
                cfg["built_in_plugins_enabled"]
            )
            loaded = plugins.load()
            tool_names = set(loaded.tools().keys())
    assert "web_search" in tool_names, tool_names
    assert "web_search_instant" in tool_names, tool_names
    print(f"✓ plugin loads by default; web-search tools present")


def _check_tool_wrapper_returns_string() -> None:
    """`web_search` returns str on success — no Attachment construction
    in the plugin (the auto-offload path handles large outputs)."""
    cap = _make_fake_api()
    assert set(cap["tools"].keys()) == {"web_search", "web_search_instant"}, cap["tools"]
    web_search = cap["tools"]["web_search"]

    fixture = [
        web_search_mod.SearchResult(
            title=r["title"], url=r["href"], snippet=r["body"]
        )
        for r in _FIXTURE_RESULTS_RAW
    ]
    with mock.patch.object(
        web_search_mod, "ddg_text_search", return_value=fixture
    ) as m:
        out = web_search("python http", n=3)
    # The wrapper now passes retry/backoff/backend kwargs through —
    # check the positional and the new kwargs ride together.
    assert m.call_count == 1, m.call_args_list
    args, kwargs = m.call_args
    assert args == ("python http",), args
    assert kwargs.get("n") == 3, kwargs
    assert "attempts" in kwargs and "backoff_s" in kwargs and "backend" in kwargs, kwargs
    assert isinstance(out, str), type(out)
    assert "Best Python HTTP libraries 2025" in out, out
    print("✓ web_search tool returns str (auto-offload path)")


def _check_tool_wrapper_translates_errors() -> None:
    """Network/parser failure → `<search error: ...>`, not a raise."""
    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]
    web_search_instant = cap["tools"]["web_search_instant"]

    def _boom(*a, **kw):
        raise RuntimeError("network down")

    with mock.patch.object(web_search_mod, "ddg_text_search", side_effect=_boom):
        out = web_search("anything")
    assert isinstance(out, str), type(out)
    assert out.startswith("<search error:"), out
    assert "network down" in out, out

    with mock.patch.object(
        web_search_mod, "ddg_instant_answer", side_effect=_boom
    ):
        out = web_search_instant("anything")
    assert isinstance(out, str), type(out)
    assert out.startswith("<instant-answer error:"), out
    print("✓ tool wrappers translate exceptions to <... error: ...>")


def _check_tool_wrapper_validates_inputs() -> None:
    """Empty query, bad `n` → tool-result error string, not a raise."""
    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]
    web_search_instant = cap["tools"]["web_search_instant"]

    assert web_search("") == "<query is empty>"
    assert web_search("   ") == "<query is empty>"
    assert web_search_instant("") == "<query is empty>"
    bad_n = web_search("hi", n="not-a-number")
    assert bad_n.startswith("<error: n must be an integer"), bad_n
    bad_n2 = web_search("hi", n=0)
    assert bad_n2 == "<error: n must be >= 1>", bad_n2
    print("✓ tool wrappers reject empty query / bad n cleanly")


# ---- Retry / backoff / classified-failure markers ----------------


def _check_retry_succeeds_after_transient_failure() -> None:
    """A single ``DDGSException`` then success: 2 calls, sleep once,
    return the success markdown."""
    from ddgs.exceptions import DDGSException

    fixture_results = [
        {"title": "ok", "href": "https://ok.example.com", "body": "ok body"}
    ]

    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            if type(self).calls == 1:
                raise DDGSException("transient: 502 from upstream")
            return fixture_results

    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ) as m_sleep:
        out = web_search("anything")

    assert _DDGS.calls == 2, f"expected 2 attempts, got {_DDGS.calls}"
    assert m_sleep.call_count == 1, m_sleep.call_args_list
    # Default backoff is (1.0, 3.0) — first retry sleeps 1.0s.
    assert m_sleep.call_args == mock.call(1.0), m_sleep.call_args
    assert "ok.example.com" in out, out
    assert not out.startswith("<search error"), out
    print("✓ retry: transient DDGSException → succeed on retry, slept 1.0s")


def _check_retry_exhausted_marker() -> None:
    """All attempts fail → distinct ``backend unavailable`` marker."""
    from ddgs.exceptions import DDGSException

    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            raise DDGSException("upstream still flaking")

    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ) as m_sleep:
        out = web_search("anything")

    assert _DDGS.calls == 3, f"expected 3 attempts (default), got {_DDGS.calls}"
    assert m_sleep.call_count == 2, m_sleep.call_args_list
    assert m_sleep.call_args_list == [mock.call(1.0), mock.call(3.0)]
    assert out.startswith("<search error: backend unavailable"), out
    assert "after 3 attempt(s)" in out, out
    assert "upstream still flaking" in out, out
    assert "fetch_url" in out, out
    print("✓ retry: exhausted → <search error: backend unavailable after 3 attempt(s); ...>")


def _check_rate_limited_marker_no_retry() -> None:
    """``RatelimitException`` → distinct marker, NO retry."""
    from ddgs.exceptions import RatelimitException

    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            raise RatelimitException("429 too many requests")

    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ) as m_sleep:
        out = web_search("anything")

    assert _DDGS.calls == 1, f"rate-limit must not retry, got {_DDGS.calls} calls"
    assert m_sleep.call_count == 0, m_sleep.call_args_list
    assert out.startswith("<search error: rate limited"), out
    assert "429" in out, out
    print("✓ rate-limit: <search error: rate limited; ...>, no retry, no sleep")


def _check_timeout_is_retried() -> None:
    """``TimeoutException`` is retryable like ``DDGSException``."""
    from ddgs.exceptions import TimeoutException

    fixture = [{"title": "t", "href": "https://t.example.com", "body": ""}]

    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            if type(self).calls == 1:
                raise TimeoutException("timed out fetching upstream")
            return fixture

    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ):
        out = web_search("anything")
    assert _DDGS.calls == 2, _DDGS.calls
    assert "t.example.com" in out, out
    print("✓ retry: TimeoutException retried like DDGSException")


def _check_attempts_1_disables_retry() -> None:
    """``retry_attempts = 1`` config → exactly one attempt, no sleep."""
    from ddgs.exceptions import DDGSException

    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            raise DDGSException("flake")

    cap = _make_fake_api(plugin_config={"retry_attempts": 1})
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ) as m_sleep:
        out = web_search("anything")
    assert _DDGS.calls == 1, _DDGS.calls
    assert m_sleep.call_count == 0, m_sleep.call_args_list
    assert out.startswith("<search error: backend unavailable"), out
    print("✓ retry_attempts=1 disables retry entirely")


def _check_backend_config_passes_through() -> None:
    """``backend = "duckduckgo,brave"`` config reaches DDGS().text(...)."""
    fixture = [{"title": "t", "href": "https://t.example.com", "body": ""}]

    class _DDGS:
        captured_backend: str | None = None

        def text(self, query, max_results=10, backend="auto"):
            type(self).captured_backend = backend
            return fixture

    cap = _make_fake_api(plugin_config={"backend": "duckduckgo,brave"})
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS):
        web_search("anything")
    assert _DDGS.captured_backend == "duckduckgo,brave", _DDGS.captured_backend
    print("✓ backend config flows through to DDGS().text(backend=...)")


def _check_non_network_exception_not_retried() -> None:
    """A programmer error (TypeError) bypasses the retry loop and
    surfaces via the existing catch-all ``<search error: ...>`` path."""
    class _DDGS:
        calls = 0

        def text(self, query, max_results=10, backend="auto"):
            type(self).calls += 1
            raise TypeError("oops, programmer bug")

    cap = _make_fake_api()
    web_search = cap["tools"]["web_search"]

    with mock.patch("ddgs.DDGS", _DDGS), mock.patch.object(
        web_search_mod.time, "sleep"
    ) as m_sleep:
        out = web_search("anything")
    assert _DDGS.calls == 1, f"non-network error must not retry, got {_DDGS.calls}"
    assert m_sleep.call_count == 0, m_sleep.call_args_list
    assert out.startswith("<search error:"), out
    assert "programmer bug" in out, out
    # NOT the typed markers — just the catch-all.
    assert "rate limited" not in out, out
    assert "backend unavailable" not in out, out
    print("✓ non-network exceptions skip retry, hit the catch-all marker")


# ---- Register-time config validation ------------------------------


def _check_register_warnings_on_bogus_config() -> None:
    cap = _make_fake_api(plugin_config={"retry_attempts": "three"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("retry_attempts must be a positive integer" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"retry_attempts": 0})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("retry_attempts must be >= 1" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"retry_backoff_s": "not-a-list"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("retry_backoff_s must be a list" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"retry_backoff_s": [1.0, -1.0, "bad"]})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("retry_backoff_s contains invalid entries" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"backend": 42})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("backend must be a string" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"backend": "   "})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("backend is set but empty" in m for m in msgs), msgs

    print("✓ register-time warnings: bogus retry/backoff/backend config flagged")


def _check_register_silent_on_clean_config() -> None:
    cap = _make_fake_api(plugin_config={
        "retry_attempts": 4,
        "retry_backoff_s": [0.5, 1.0, 2.0],
        "backend": "duckduckgo,brave,yahoo",
    })
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"expected no warnings, got: {msgs}"

    # Empty config too.
    cap = _make_fake_api(plugin_config={})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"expected no warnings on empty config, got: {msgs}"
    print("✓ register-time silent on clean and empty config")


def _check_instant_answer_uses_fixture_http() -> None:
    """`ddg_instant_answer` parses a fixture HTTP body without touching
    the network. Mocks at urllib.request.urlopen."""
    body = json.dumps(_FIXTURE_INSTANT_PAYLOAD).encode("utf-8")

    class _FakeResp:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.headers = self  # has get_content_charset()

        def get_content_charset(self, default="utf-8"):
            return "utf-8"

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with mock.patch.object(
        web_search_mod.urllib.request,
        "urlopen",
        return_value=_FakeResp(body),
    ):
        ans = web_search_mod.ddg_instant_answer("python")
    assert ans.heading == "Python (programming language)", ans
    assert "high-level" in ans.abstract, ans
    print("✓ ddg_instant_answer parses fixture HTTP body")


def main() -> None:
    _check_search_formatter()
    _check_search_formatter_empty()
    _check_instant_parser_picks_abstract()
    _check_instant_formatter_default()
    _check_instant_formatter_with_related()
    _check_instant_formatter_answer_beats_abstract()
    _check_instant_formatter_no_data()
    _check_plugin_loads_under_default_config()
    _check_tool_wrapper_returns_string()
    _check_tool_wrapper_translates_errors()
    _check_tool_wrapper_validates_inputs()
    _check_retry_succeeds_after_transient_failure()
    _check_retry_exhausted_marker()
    _check_rate_limited_marker_no_retry()
    _check_timeout_is_retried()
    _check_attempts_1_disables_retry()
    _check_backend_config_passes_through()
    _check_non_network_exception_not_retried()
    _check_register_warnings_on_bogus_config()
    _check_register_silent_on_clean_config()
    _check_instant_answer_uses_fixture_http()
    print("smoke_web_search: all checks passed")


if __name__ == "__main__":
    main()
