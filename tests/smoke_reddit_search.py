"""End-to-end smoke for the reddit-search plugin.

Concerns covered:

  1. **Plugin loads under the default config.** With "reddit-search"
     in `built_in_plugins_enabled`, `discover()` and `load()` produce
     the `reddit_search` tool.
  2. **Result formatter renders posts as markdown.** Numbered list,
     subreddit / score / comments meta line, permalink + external URL
     when distinct, selftext excerpt when present.
  3. **Empty results → ``<no reddit results for ...>`` marker.**
  4. **Listing parser tolerates partial / weird payloads.** Algolia
     and Reddit both occasionally return numeric scores as strings
     or omit fields entirely.
  5. **URL builder respects subreddit / time_window / sort knobs.**
     ``restrict_sr=1`` only when subreddit is set, time/sort flow
     through.
  6. **Plugin returns Attachment on success.** Markdown rides
     inline_text, structured JSON list rides content, suffix ".json".
  7. **save_structured = false** preserves the legacy markdown-only
     string return.
  8. **Empty results return string** (no attachment for an empty list).
  9. **Validation paths** return string markers, never raise:
     empty query, bad ``n``, bad ``time_window``, bad ``sort``,
     bad ``subreddit``.
 10. **HTTP failures translate cleanly:** 429 → distinct rate-limit
     marker; other HTTPError → ``<reddit-search error: HTTP N: ...>``;
     URLError (network failure) → ``<reddit-search error: network
     failure: ...>``.
 11. **Register-time warnings** on bogus `timeout_s`, `user_agent`,
     `save_structured` config; silent on clean config.

Run with:
    .venv/bin/python -m tests.smoke_reddit_search
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

from pyagent import config as config_mod, paths as paths_mod, plugins
from pyagent.plugins.reddit_search import (
    RedditPost,
    _build_url,
    _parse_listing,
    format_results,
)
from pyagent.plugins.reddit_search import register as reddit_register
from pyagent.plugins import reddit_search as reddit_mod


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

    reddit_register(_FakeAPI())
    return captured


_FIXTURE_LISTING_PAYLOAD = {
    "kind": "Listing",
    "data": {
        "after": "t3_xyz",
        "children": [
            {
                "kind": "t3",
                "data": {
                    "title": "How do you handle Python deps in 2026?",
                    "url": "https://www.example.com/article",
                    "permalink": "/r/Python/comments/abc/how_do_you_handle/",
                    "subreddit": "Python",
                    "author": "userone",
                    "score": 142,
                    "num_comments": 38,
                    "created_utc": 1714765432.0,
                    "selftext": "Coming from Java, what's the consensus on uv vs poetry vs hatch?",
                },
            },
            {
                "kind": "t3",
                "data": {
                    "title": "(no title example)",
                    "url": "https://www.reddit.com/r/Python/comments/def/",
                    "permalink": "/r/Python/comments/def/",
                    "subreddit": "Python",
                    "author": "usertwo",
                    "score": "12",       # string-typed score; parser must coerce
                    "num_comments": None,  # missing
                    "selftext": "",
                },
            },
        ],
    },
}


def _check_plugin_loads_under_default_config() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-reddit-"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
            assert "reddit-search" in cfg["built_in_plugins_enabled"], (
                cfg["built_in_plugins_enabled"]
            )
            loaded = plugins.load()
            tool_names = set(loaded.tools().keys())
    assert "reddit_search" in tool_names, tool_names
    print("✓ plugin loads by default; reddit_search tool present")


def _check_format_results() -> None:
    posts = _parse_listing(_FIXTURE_LISTING_PAYLOAD)
    md = format_results(posts, "python deps", subreddit=None)
    assert "# Reddit results for 'python deps'" in md, md
    assert "1. **How do you handle Python deps in 2026?**" in md, md
    assert "r/Python" in md, md
    assert "u/userone" in md, md
    assert "142 pts" in md, md
    assert "38 comments" in md, md
    assert "https://www.reddit.com/r/Python/comments/abc/" in md, md
    # External link distinct from permalink → "link:" line shown.
    assert "link: https://www.example.com/article" in md, md
    # Selftext excerpt carried through.
    assert "uv vs poetry vs hatch" in md, md
    # Second post: same URL as permalink → no "link:" line.
    second_block = md.split("2. ", 1)[1]
    assert "link:" not in second_block.split("\n3. ", 1)[0], second_block
    # String-typed score coerced.
    assert "12 pts" in md, md
    print("✓ format_results: numbered markdown with metadata + selftext excerpt")


def _check_format_results_empty() -> None:
    md = format_results([], "obscure", subreddit=None)
    assert md.startswith("<no reddit results"), md
    md_sub = format_results([], "obscure", subreddit="Python")
    assert "in r/Python" in md_sub, md_sub
    print("✓ format_results: empty → <no reddit results ...> with sub scope")


def _check_parse_listing_tolerates_weird_payloads() -> None:
    posts = _parse_listing({"data": {"children": []}})
    assert posts == []
    posts = _parse_listing({"not": "the right shape"})
    assert posts == []
    # Score / num_comments coercion already covered by the fixture;
    # also confirm missing permalink doesn't crash.
    payload = {
        "data": {
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "title": "x",
                        "url": "https://example.com/x",
                        "permalink": "",
                        "subreddit": "",
                        "author": "",
                        "score": 0,
                    },
                }
            ]
        }
    }
    posts = _parse_listing(payload)
    assert len(posts) == 1, posts
    assert posts[0].permalink == "", posts[0]
    print("✓ _parse_listing: tolerates missing permalink / weird shapes")


def _check_url_builder() -> None:
    url = _build_url(
        "python deps", n=10, subreddit=None, time_window="all", sort="relevance",
    )
    assert url.startswith("https://www.reddit.com/search.json?"), url
    assert "q=python+deps" in url or "q=python%20deps" in url, url
    assert "limit=10" in url, url
    assert "t=all" in url, url
    assert "sort=relevance" in url, url
    assert "restrict_sr" not in url, url

    url = _build_url(
        "kdb", n=5, subreddit="kdb", time_window="month", sort="top",
    )
    assert "https://www.reddit.com/r/kdb/search.json?" in url, url
    assert "limit=5" in url, url
    assert "t=month" in url, url
    assert "sort=top" in url, url
    assert "restrict_sr=1" in url, url
    print("✓ URL builder: subreddit / time_window / sort all flow through")


def _check_tool_returns_attachment() -> None:
    from pyagent.session import Attachment as _Attachment

    cap = _make_fake_api()
    reddit_search = cap["tools"]["reddit_search"]

    fixture_posts = _parse_listing(_FIXTURE_LISTING_PAYLOAD)
    with mock.patch.object(
        reddit_mod, "reddit_text_search", return_value=fixture_posts
    ) as m:
        out = reddit_search("python deps", n=2, subreddit="Python")

    args, kwargs = m.call_args
    assert args == ("python deps",), args
    assert kwargs.get("n") == 2, kwargs
    assert kwargs.get("subreddit") == "Python", kwargs
    assert "timeout_s" in kwargs, kwargs
    assert "user_agent" in kwargs, kwargs

    assert isinstance(out, _Attachment), type(out)
    assert out.suffix == ".json", out.suffix
    assert "Best Python HTTP libraries" not in out.inline_text, out.inline_text
    assert "How do you handle Python deps" in out.inline_text, out.inline_text
    parsed = json.loads(out.content)
    assert isinstance(parsed, list) and len(parsed) == 2, parsed
    assert parsed[0]["title"].startswith("How do you handle"), parsed[0]
    assert parsed[0]["score"] == 142, parsed[0]
    assert parsed[0]["subreddit"] == "Python", parsed[0]
    print("✓ tool returns Attachment(inline_text=md, content=json)")


def _check_save_structured_disabled_returns_string() -> None:
    cap = _make_fake_api(plugin_config={"save_structured": False})
    reddit_search = cap["tools"]["reddit_search"]
    fixture = _parse_listing(_FIXTURE_LISTING_PAYLOAD)
    with mock.patch.object(
        reddit_mod, "reddit_text_search", return_value=fixture
    ):
        out = reddit_search("python deps")
    assert isinstance(out, str), type(out)
    assert "How do you handle Python deps" in out, out
    assert "[also saved:" not in out, out
    print("✓ save_structured=false: legacy markdown-only string return")


def _check_empty_results_returns_string() -> None:
    cap = _make_fake_api()
    reddit_search = cap["tools"]["reddit_search"]
    with mock.patch.object(
        reddit_mod, "reddit_text_search", return_value=[]
    ):
        out = reddit_search("nonsense query no hits")
    assert isinstance(out, str), type(out)
    assert out.startswith("<no reddit results "), out
    print("✓ empty results: <no reddit results ...> string marker, no attachment")


def _check_validation_paths() -> None:
    cap = _make_fake_api()
    reddit_search = cap["tools"]["reddit_search"]
    assert reddit_search("") == "<query is empty>"
    assert reddit_search("   ") == "<query is empty>"
    bad_n = reddit_search("hi", n="not-a-number")
    assert bad_n.startswith("<error: n must be an integer"), bad_n
    bad_n2 = reddit_search("hi", n=0)
    assert bad_n2 == "<error: n must be >= 1>", bad_n2
    bad_sub = reddit_search("hi", subreddit="")
    assert bad_sub.startswith("<error: subreddit must be a non-empty"), bad_sub
    bad_window = reddit_search("hi", time_window="century")
    assert bad_window.startswith("<error: time_window must be one of"), bad_window
    bad_sort = reddit_search("hi", sort="confusion")
    assert bad_sort.startswith("<error: sort must be one of"), bad_sort
    # Subreddit shape validation — typos like "Python/comments/abc"
    # used to silently produce a 404 URL. Reject up front per #94 review.
    bad_shape = reddit_search("hi", subreddit="Python/comments/abc")
    assert bad_shape.startswith(
        "<error: subreddit must be alphanumeric"
    ), bad_shape
    bad_long = reddit_search("hi", subreddit="x" * 22)
    assert bad_long.startswith(
        "<error: subreddit must be alphanumeric"
    ), bad_long
    bad_punct = reddit_search("hi", subreddit="r-with-dashes")
    assert bad_punct.startswith(
        "<error: subreddit must be alphanumeric"
    ), bad_punct
    print(
        "✓ validation: empty / bad n / bad subreddit / bad window / "
        "bad sort / bad subreddit-shape"
    )


def _check_subreddit_normalization() -> None:
    """``r/Python``, ``/r/Python``, and ``Python`` all resolve to
    a request against /r/Python/search.json."""
    cap = _make_fake_api()
    reddit_search = cap["tools"]["reddit_search"]
    fixture = _parse_listing(_FIXTURE_LISTING_PAYLOAD)
    captured_subs: list[str | None] = []

    def _fake(query, *, n, subreddit, time_window, sort, timeout_s, user_agent):
        captured_subs.append(subreddit)
        return fixture

    with mock.patch.object(reddit_mod, "reddit_text_search", side_effect=_fake):
        reddit_search("hi", subreddit="r/Python")
        reddit_search("hi", subreddit="/r/Python")
        reddit_search("hi", subreddit="Python")
    assert captured_subs == ["Python", "Python", "Python"], captured_subs
    print("✓ subreddit normalization: r/, /r/, bare-name all flatten to bare")


def _check_http_failures_translate() -> None:
    cap = _make_fake_api()
    reddit_search = cap["tools"]["reddit_search"]

    def _http_429(*a, **kw):
        raise urllib.error.HTTPError(
            "https://www.reddit.com/search.json",
            429,
            "Too Many Requests",
            {},
            None,
        )

    with mock.patch.object(reddit_mod, "reddit_text_search", side_effect=_http_429):
        out = reddit_search("anything")
    assert out.startswith("<reddit-search error: rate limited"), out
    assert "429" in out, out
    # Message wording per #94 review: drop the misleading
    # "set a more identifying user_agent" suggestion. New wording
    # explicitly says UA *isn't* the fix, so we assert on the
    # corrective shape, not on the literal token "user_agent".
    assert "set a more identifying" not in out, out
    assert "pacing or OAuth" in out, out

    def _http_500(*a, **kw):
        raise urllib.error.HTTPError(
            "https://www.reddit.com/search.json",
            500,
            "Internal Server Error",
            {},
            None,
        )

    with mock.patch.object(reddit_mod, "reddit_text_search", side_effect=_http_500):
        out = reddit_search("anything")
    assert out.startswith("<reddit-search error: HTTP 500"), out

    def _url_err(*a, **kw):
        raise urllib.error.URLError("DNS down")

    with mock.patch.object(reddit_mod, "reddit_text_search", side_effect=_url_err):
        out = reddit_search("anything")
    assert out.startswith("<reddit-search error: network failure"), out
    assert "DNS down" in out, out

    def _generic(*a, **kw):
        raise RuntimeError("totally unexpected")

    with mock.patch.object(reddit_mod, "reddit_text_search", side_effect=_generic):
        out = reddit_search("anything")
    assert out.startswith("<reddit-search error: "), out
    assert "totally unexpected" in out, out
    print("✓ HTTP failures: 429 / 500 / URLError / generic all translate")


def _check_register_warnings() -> None:
    cap = _make_fake_api(plugin_config={"timeout_s": "ten"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be a positive integer" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"timeout_s": -3})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be a positive integer" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"user_agent": ""})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("user_agent" in m for m in msgs), msgs

    cap = _make_fake_api(plugin_config={"save_structured": "yes"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("save_structured must be a bool" in m for m in msgs), msgs

    # Clean config: silent.
    cap = _make_fake_api(plugin_config={
        "timeout_s": 15,
        "user_agent": "myagent/1.0",
        "save_structured": True,
    })
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], msgs
    print("✓ register-time warnings: bogus configs flagged, clean config silent")


def main() -> None:
    _check_plugin_loads_under_default_config()
    _check_format_results()
    _check_format_results_empty()
    _check_parse_listing_tolerates_weird_payloads()
    _check_url_builder()
    _check_tool_returns_attachment()
    _check_save_structured_disabled_returns_string()
    _check_empty_results_returns_string()
    _check_validation_paths()
    _check_subreddit_normalization()
    _check_http_failures_translate()
    _check_register_warnings()
    print("smoke_reddit_search: all checks passed")


if __name__ == "__main__":
    main()
