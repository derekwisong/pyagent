"""End-to-end smoke for the web-search plugin.

Four concerns:

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
     The agent's auto-offload path expects strings; both tools must
     return `str` on success and on every error path. A `web_search`
     network failure becomes `<search error: ...>` rather than
     bubbling out of the tool.

Run with:

    .venv/bin/python -m tests.smoke_web_search
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from pyagent import config as config_mod, paths as paths_mod, plugins
from pyagent.plugins.web_search import search as web_search_mod


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
    # Re-register through a fake API so we can capture the tool fn.
    captured: dict[str, callable] = {}

    class _FakeAPI:
        def register_tool(self, name, fn):
            captured[name] = fn

    from pyagent.plugins.web_search import register

    register(_FakeAPI())
    assert set(captured.keys()) == {"web_search", "web_search_instant"}, captured

    web_search = captured["web_search"]

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
    m.assert_called_once_with("python http", n=3)
    assert isinstance(out, str), type(out)
    assert "Best Python HTTP libraries 2025" in out, out
    print("✓ web_search tool returns str (auto-offload path)")


def _check_tool_wrapper_translates_errors() -> None:
    """Network/parser failure → `<search error: ...>`, not a raise."""
    captured: dict[str, callable] = {}

    class _FakeAPI:
        def register_tool(self, name, fn):
            captured[name] = fn

    from pyagent.plugins.web_search import register

    register(_FakeAPI())
    web_search = captured["web_search"]
    web_search_instant = captured["web_search_instant"]

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
    captured: dict[str, callable] = {}

    class _FakeAPI:
        def register_tool(self, name, fn):
            captured[name] = fn

    from pyagent.plugins.web_search import register

    register(_FakeAPI())
    web_search = captured["web_search"]
    web_search_instant = captured["web_search_instant"]

    assert web_search("") == "<query is empty>"
    assert web_search("   ") == "<query is empty>"
    assert web_search_instant("") == "<query is empty>"
    bad_n = web_search("hi", n="not-a-number")
    assert bad_n.startswith("<error: n must be an integer"), bad_n
    bad_n2 = web_search("hi", n=0)
    assert bad_n2 == "<error: n must be >= 1>", bad_n2
    print("✓ tool wrappers reject empty query / bad n cleanly")


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
    _check_instant_answer_uses_fixture_http()
    print("smoke_web_search: all checks passed")


if __name__ == "__main__":
    main()
