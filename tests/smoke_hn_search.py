"""End-to-end smoke for the hn-search plugin.

Concerns covered:

  1. **Plugin loads under the default config.** With "hn-search" in
     `built_in_plugins_enabled`, `discover()` and `load()` produce
     the `hn_search` tool.
  2. **Result formatter renders stories as markdown.** Numbered list,
     author / points / comments / date meta line, permalink + external
     URL when distinct.
  3. **Empty results → ``<no hn results for ...>`` marker.**
  4. **Hits parser tolerates partial / weird payloads.** Algolia's
     index occasionally omits fields or returns numeric scores as
     strings; the parser must coerce, not crash.
  5. **URL builder respects kind / time_window / min_points knobs.**
  6. **Plugin returns Attachment on success.** Markdown rides
     inline_text, structured JSON list rides content, suffix ".json".
  7. **save_structured = false** preserves the legacy markdown-only
     string return.
  8. **Empty results return string** (no attachment for an empty list).
  9. **Validation paths** return string markers, never raise: empty
     query, bad ``n``, bad ``kind``, bad ``time_window``, bad
     ``min_points``.
 10. **HTTP failures translate cleanly:** HTTPError →
     ``<hn-search error: HTTP N: ...>``; URLError → network failure
     marker; generic → catch-all marker.
 11. **Register-time warnings** on bogus `timeout_s` and
     `save_structured` config; silent on clean config.

Run with:
    .venv/bin/python -m tests.smoke_hn_search
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

from pyagent import config as config_mod, paths as paths_mod, plugins
from pyagent.plugins.hn_search import (
    HNStory,
    _build_url,
    _parse_hits,
    format_results,
)
from pyagent.plugins.hn_search import register as hn_register
from pyagent.plugins import hn_search as hn_mod


def _make_fake_api(plugin_config: dict | None = None) -> dict:
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

    hn_register(_FakeAPI())
    return captured


_FIXTURE_HITS_PAYLOAD = {
    "hits": [
        {
            "title": "Why we switched from Postgres to SQLite",
            "url": "https://eng.example.com/postgres-sqlite",
            "objectID": "12345678",
            "author": "alice",
            "points": 412,
            "num_comments": 198,
            "created_at": "2026-04-22T14:03:00.000Z",
            "_tags": ["story", "author_alice", "story_12345678"],
        },
        {
            "title": "Ask HN: How do you onboard senior engineers?",
            # Ask HN: url is None — discussion IS the content
            "url": None,
            "objectID": "98765432",
            "author": "bob",
            "points": "57",  # string-typed score; parser must coerce
            "num_comments": None,  # missing
            "created_at": "2026-05-01T09:18:00.000Z",
            "_tags": ["story", "ask_hn", "author_bob", "story_98765432"],
        },
    ],
    "nbHits": 2,
    "page": 0,
    "nbPages": 1,
    "hitsPerPage": 10,
}


def _check_plugin_loads_under_default_config() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-hn-"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
            assert "hn-search" in cfg["built_in_plugins_enabled"], (
                cfg["built_in_plugins_enabled"]
            )
            loaded = plugins.load()
            tool_names = set(loaded.tools().keys())
    assert "hn_search" in tool_names, tool_names
    print("✓ plugin loads by default; hn_search tool present")


def _check_format_results() -> None:
    stories = _parse_hits(_FIXTURE_HITS_PAYLOAD)
    md = format_results(stories, "postgres sqlite")
    assert "# Hacker News results for 'postgres sqlite'" in md, md
    assert "1. **Why we switched from Postgres to SQLite**" in md, md
    assert "by alice" in md, md
    assert "412 pts" in md, md
    assert "198 comments" in md, md
    assert "2026-04-22" in md, md
    assert "https://news.ycombinator.com/item?id=12345678" in md, md
    # External link distinct from permalink → "link:" line shown.
    assert "link: https://eng.example.com/postgres-sqlite" in md, md
    # Ask HN: url == permalink → no "link:" line.
    second_block = md.split("2. ", 1)[1]
    assert "link:" not in second_block, second_block
    # String-typed score coerced.
    assert "57 pts" in md, md
    print("✓ format_results: numbered markdown with metadata; Ask HN handled")


def _check_format_results_empty() -> None:
    md = format_results([], "obscure")
    assert md.startswith("<no hn results"), md
    print("✓ format_results: empty → <no hn results ...> marker")


def _check_parse_hits_tolerates_weird_payloads() -> None:
    assert _parse_hits({"hits": []}) == []
    assert _parse_hits({"not": "the right shape"}) == []
    payload = {
        "hits": [
            {
                "title": "x",
                "objectID": "777",
                # missing author / points / etc.
                "_tags": ["comment", "author_carol", "story_42"],
            }
        ]
    }
    stories = _parse_hits(payload)
    assert len(stories) == 1, stories
    assert stories[0].permalink == "https://news.ycombinator.com/item?id=777", stories[0]
    assert stories[0].points == 0, stories[0]
    assert stories[0].num_comments == 0, stories[0]
    assert stories[0].type == "comment", stories[0]
    print("✓ _parse_hits: tolerates missing fields; type tag picked correctly")


def _check_parse_hits_type_tag_precedence() -> None:
    """Lock the chosen precedence on `_tags = [story, ask_hn, ...]`.
    The first non-author/non-id tag wins — currently `story` over
    `ask_hn`. If that flips, the smoke fails loudly and the reviewer
    can decide whether the new precedence is intentional."""
    payload = {
        "hits": [
            {
                "title": "Ask HN: …",
                "objectID": "1",
                "_tags": ["story", "ask_hn", "author_a", "story_1"],
            },
            {
                "title": "Show HN: …",
                "objectID": "2",
                "_tags": ["story", "show_hn", "author_b", "story_2"],
            },
            {
                "title": "Job posting",
                "objectID": "3",
                "_tags": ["job", "author_c"],
            },
        ]
    }
    stories = _parse_hits(payload)
    # `story` precedes `ask_hn` / `show_hn` in the list → it wins.
    # That's the chosen design: every Ask/Show HN is fundamentally a
    # story; the kind tag is what `kind=story` filtered on. If a
    # consumer wanted to differentiate, they'd inspect the title.
    assert stories[0].type == "story", stories[0]
    assert stories[1].type == "story", stories[1]
    assert stories[2].type == "job", stories[2]
    print("✓ _parse_hits: type precedence — story wins over ask_hn/show_hn")


def _check_url_builder() -> None:
    url = _build_url(
        "postgres sqlite", n=10, kind="story", time_window="all", min_points=None,
    )
    assert url.startswith("https://hn.algolia.com/api/v1/search?"), url
    assert "query=postgres+sqlite" in url or "query=postgres%20sqlite" in url, url
    assert "hitsPerPage=10" in url, url
    assert "tags=story" in url, url
    # No min_points and time_window=all → no numericFilters.
    assert "numericFilters" not in url, url

    # Mock time so the epoch cutoff is deterministic.
    fake_now = 1_750_000_000  # arbitrary fixed instant
    with mock.patch.object(hn_mod.time, "time", return_value=fake_now):
        url = _build_url("rust", n=5, kind="any", time_window="month", min_points=50)
    assert "hitsPerPage=5" in url, url
    # kind=any drops the tags filter.
    assert "tags=story" not in url, url
    assert "numericFilters" in url, url
    assert "points%3E%3D50" in url, url  # urlencoded ">=50"
    # NEW: time window resolves to a literal numeric epoch (not the
    # broken `now-1M` syntax that Algolia rejects with HTTP 400).
    expected_cutoff = fake_now - 2_592_000  # month = 30 days
    assert f"created_at_i%3E{expected_cutoff}" in url, url
    assert "now-" not in url, (
        "regression: time window must resolve to a literal epoch, "
        "not the relative-time string Algolia rejects"
    )
    print("✓ URL builder: kind / time_window (numeric epoch) / min_points flow through")


def _check_time_window_filter_helper() -> None:
    """`_time_window_filter` produces literal-epoch filters that
    Algolia accepts. Locks in the #94 review fix — the previous
    `now-1d`-style strings returned HTTP 400 from Algolia."""
    fake_now = 1_750_000_000
    with mock.patch.object(hn_mod.time, "time", return_value=fake_now):
        assert hn_mod._time_window_filter("all") == ""
        assert hn_mod._time_window_filter("hour") == f"created_at_i>{fake_now - 3600}"
        assert hn_mod._time_window_filter("day") == f"created_at_i>{fake_now - 86400}"
        assert hn_mod._time_window_filter("week") == f"created_at_i>{fake_now - 604800}"
        assert hn_mod._time_window_filter("month") == f"created_at_i>{fake_now - 2_592_000}"
        assert hn_mod._time_window_filter("year") == f"created_at_i>{fake_now - 31_536_000}"
        # Unknown window → empty (defensive; the tool layer validates first)
        assert hn_mod._time_window_filter("century") == ""
    print("✓ time_window: produces literal numeric epochs Algolia accepts")


def _check_tool_returns_attachment() -> None:
    from pyagent.session import Attachment as _Attachment

    cap = _make_fake_api()
    hn_search = cap["tools"]["hn_search"]

    fixture_stories = _parse_hits(_FIXTURE_HITS_PAYLOAD)
    with mock.patch.object(
        hn_mod, "hn_text_search", return_value=fixture_stories
    ) as m:
        out = hn_search("postgres sqlite", n=2, kind="story")

    args, kwargs = m.call_args
    assert args == ("postgres sqlite",), args
    assert kwargs.get("n") == 2, kwargs
    assert kwargs.get("kind") == "story", kwargs
    assert "timeout_s" in kwargs, kwargs

    assert isinstance(out, _Attachment), type(out)
    assert out.suffix == ".json", out.suffix
    assert "Why we switched from Postgres to SQLite" in out.inline_text, out.inline_text
    parsed = json.loads(out.content)
    assert isinstance(parsed, list) and len(parsed) == 2, parsed
    assert parsed[0]["title"].startswith("Why we switched"), parsed[0]
    assert parsed[0]["points"] == 412, parsed[0]
    assert parsed[0]["object_id"] == "12345678", parsed[0]
    print("✓ tool returns Attachment(inline_text=md, content=json)")


def _check_save_structured_disabled_returns_string() -> None:
    cap = _make_fake_api(plugin_config={"save_structured": False})
    hn_search = cap["tools"]["hn_search"]
    fixture = _parse_hits(_FIXTURE_HITS_PAYLOAD)
    with mock.patch.object(hn_mod, "hn_text_search", return_value=fixture):
        out = hn_search("postgres sqlite")
    assert isinstance(out, str), type(out)
    assert "Why we switched" in out, out
    assert "[also saved:" not in out, out
    print("✓ save_structured=false: legacy markdown-only string return")


def _check_empty_results_returns_string() -> None:
    cap = _make_fake_api()
    hn_search = cap["tools"]["hn_search"]
    with mock.patch.object(hn_mod, "hn_text_search", return_value=[]):
        out = hn_search("nonsense query no hits")
    assert isinstance(out, str), type(out)
    assert out.startswith("<no hn results "), out
    print("✓ empty results: <no hn results ...> string marker, no attachment")


def _check_validation_paths() -> None:
    cap = _make_fake_api()
    hn_search = cap["tools"]["hn_search"]
    assert hn_search("") == "<query is empty>"
    assert hn_search("   ") == "<query is empty>"
    bad_n = hn_search("hi", n="not-a-number")
    assert bad_n.startswith("<error: n must be an integer"), bad_n
    bad_n2 = hn_search("hi", n=0)
    assert bad_n2 == "<error: n must be >= 1>", bad_n2
    bad_kind = hn_search("hi", kind="article")
    assert bad_kind.startswith("<error: kind must be one of"), bad_kind
    bad_window = hn_search("hi", time_window="century")
    assert bad_window.startswith("<error: time_window must be one of"), bad_window
    bad_pts = hn_search("hi", min_points="lots")
    assert bad_pts.startswith("<error: min_points must be an integer"), bad_pts
    bad_pts2 = hn_search("hi", min_points=-5)
    assert bad_pts2.startswith("<error: min_points must be >= 0"), bad_pts2
    print("✓ validation: empty / bad n / bad kind / bad window / bad min_points")


def _check_http_failures_translate() -> None:
    cap = _make_fake_api()
    hn_search = cap["tools"]["hn_search"]

    def _http_500(*a, **kw):
        raise urllib.error.HTTPError(
            "https://hn.algolia.com/api/v1/search",
            500,
            "Internal Server Error",
            {},
            None,
        )

    with mock.patch.object(hn_mod, "hn_text_search", side_effect=_http_500):
        out = hn_search("anything")
    assert out.startswith("<hn-search error: HTTP 500"), out

    def _url_err(*a, **kw):
        raise urllib.error.URLError("DNS down")

    with mock.patch.object(hn_mod, "hn_text_search", side_effect=_url_err):
        out = hn_search("anything")
    assert out.startswith("<hn-search error: network failure"), out

    def _generic(*a, **kw):
        raise RuntimeError("totally unexpected")

    with mock.patch.object(hn_mod, "hn_text_search", side_effect=_generic):
        out = hn_search("anything")
    assert out.startswith("<hn-search error: "), out
    assert "totally unexpected" in out, out
    print("✓ HTTP failures: HTTPError / URLError / generic all translate")


def _check_register_warnings() -> None:
    cap = _make_fake_api(plugin_config={"timeout_s": "ten"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be a positive integer" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"timeout_s": 0})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be a positive integer" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"save_structured": "no"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("save_structured must be a bool" in m for m in msgs), msgs

    # Clean config: silent.
    cap = _make_fake_api(plugin_config={
        "timeout_s": 15,
        "save_structured": True,
    })
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], msgs
    print("✓ register-time warnings: bogus configs flagged, clean config silent")


def main() -> None:
    _check_plugin_loads_under_default_config()
    _check_format_results()
    _check_format_results_empty()
    _check_parse_hits_tolerates_weird_payloads()
    _check_parse_hits_type_tag_precedence()
    _check_url_builder()
    _check_time_window_filter_helper()
    _check_tool_returns_attachment()
    _check_save_structured_disabled_returns_string()
    _check_empty_results_returns_string()
    _check_validation_paths()
    _check_http_failures_translate()
    _check_register_warnings()
    print("smoke_hn_search: all checks passed")


if __name__ == "__main__":
    main()
