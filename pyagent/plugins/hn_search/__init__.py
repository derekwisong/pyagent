"""hn-search — bundled plugin: search Hacker News via Algolia.

One tool, ``hn_search(query, n=10, kind="story", time_window="all",
min_points=None)``. Same return shape as web_search and reddit_search:
Attachment(inline_text=<markdown>, content=<JSON list>, suffix=".json")
on success, plain string markers on miss / failure.

The HN search index Algolia hosts at ``hn.algolia.com/api/v1`` is
the same one HN's own Search box uses. Public, no auth, no UA
requirement. Different shape from Reddit: HN items have points,
author, comment count, created_at, type (story / comment / job).

Why a separate plugin: HN's "consensus on this library / startup /
technical claim" content sits poorly in generic web search results
(landing pages and SEO outrank discussion threads). For technical
research workflows, this is the gap that matters.

Configuration (all optional, all read at call time):

  ``[plugins.hn-search]`` table in pyagent's config TOML
    ``timeout_s`` — HTTP timeout per attempt. Default 10.
    ``save_structured`` — when true (default), save the JSON list
        as an attachment alongside the markdown. Set to false for
        a legacy markdown-only string return.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pyagent.session import Attachment

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 10
_DEFAULT_SAVE_STRUCTURED = True
_MAX_RESULTS = 50  # Algolia's per-page max; HN search results are short, no need to cap lower
_VALID_KINDS = {"story", "comment", "poll", "any"}
# Time-window seconds. Algolia's numericFilters take literal numeric
# epochs, NOT relative-time strings — earlier `now-1d`-style values
# returned HTTP 400 and silently broke every non-`all` filter
# (caught in #94 review). The filter is computed at call time as
# ``int(time.time()) - <seconds>`` so the agent always asks about
# "the last N from this moment." 0 means "no filter."
_TIME_WINDOW_SECONDS: dict[str, int] = {
    "all": 0,
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,    # 30 days, by convention
    "year": 31536000,    # 365 days
}
_VALID_TIME_WINDOWS = set(_TIME_WINDOW_SECONDS.keys())


def _time_window_filter(window: str) -> str:
    """Build the Algolia ``created_at_i>EPOCH`` filter for a time
    window. Returns ``""`` for ``all`` (skip the filter entirely)."""
    seconds = _TIME_WINDOW_SECONDS.get(window, 0)
    if seconds <= 0:
        return ""
    cutoff = int(time.time()) - seconds
    return f"created_at_i>{cutoff}"


@dataclass(frozen=True)
class HNStory:
    """One HN search result, normalized."""

    title: str
    url: str           # external URL the story links to (or hn-permalink for Ask/Show)
    permalink: str     # https://news.ycombinator.com/item?id=<id> — always present
    author: str
    points: int
    num_comments: int
    created_at: str    # ISO 8601, as Algolia returns it
    object_id: str
    type: str          # story / comment / poll / job


def _resolve_timeout(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("timeout_s")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _DEFAULT_TIMEOUT_S


def _resolve_save_structured(plugin_cfg: dict) -> bool:
    raw = plugin_cfg.get("save_structured")
    if isinstance(raw, bool):
        return raw
    return _DEFAULT_SAVE_STRUCTURED


def _config_warnings(plugin_cfg: dict) -> list[str]:
    out: list[str] = []
    if "timeout_s" in plugin_cfg:
        raw = plugin_cfg["timeout_s"]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
            out.append(
                f"timeout_s must be a positive integer, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_TIMEOUT_S}"
            )
    if "save_structured" in plugin_cfg:
        raw = plugin_cfg["save_structured"]
        if not isinstance(raw, bool):
            out.append(
                f"save_structured must be a bool, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_SAVE_STRUCTURED}"
            )
    return out


def _build_url(
    query: str,
    n: int,
    kind: str,
    time_window: str,
    min_points: int | None,
) -> str:
    base = "https://hn.algolia.com/api/v1/search"
    params: dict[str, str] = {
        "query": query,
        "hitsPerPage": str(n),
    }
    # Algolia tag values: story, comment, poll, pollopt, show_hn,
    # ask_hn, front_page, job, user. ``any`` removes the filter.
    if kind != "any":
        params["tags"] = kind
    numeric: list[str] = []
    if min_points is not None and min_points > 0:
        numeric.append(f"points>={min_points}")
    win = _time_window_filter(time_window)
    if win:
        numeric.append(win)
    if numeric:
        params["numericFilters"] = ",".join(numeric)
    return f"{base}?{urllib.parse.urlencode(params)}"


def _parse_hits(payload: dict) -> list[HNStory]:
    hits = payload.get("hits") or []
    out: list[HNStory] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        object_id = str(hit.get("objectID") or "").strip()
        permalink = (
            f"https://news.ycombinator.com/item?id={object_id}"
            if object_id
            else ""
        )
        # `url` is None for Ask HN / Show HN where the discussion
        # IS the content. Fall back to permalink so consumers always
        # have a clickable link.
        external_url = hit.get("url") or permalink
        try:
            points = int(hit.get("points") or 0)
        except (TypeError, ValueError):
            points = 0
        try:
            num_comments = int(hit.get("num_comments") or 0)
        except (TypeError, ValueError):
            num_comments = 0
        # Algolia returns _tags like ["story", "author_xyz", "story_123"].
        # The first non-author/non-id tag is the kind.
        item_type = "story"
        for tag in hit.get("_tags") or []:
            if isinstance(tag, str) and not tag.startswith(
                ("author_", "story_", "comment_", "poll_", "job_")
            ):
                item_type = tag
                break
        out.append(
            HNStory(
                title=str(
                    hit.get("title")
                    or hit.get("story_title")
                    or ""
                ).strip(),
                url=str(external_url).strip(),
                permalink=permalink,
                author=str(hit.get("author") or "").strip(),
                points=points,
                num_comments=num_comments,
                created_at=str(hit.get("created_at") or "").strip(),
                object_id=object_id,
                type=item_type,
            )
        )
    return out


def hn_text_search(
    query: str,
    *,
    n: int = 10,
    kind: str = "story",
    time_window: str = "all",
    min_points: int | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> list[HNStory]:
    """Hit hn.algolia.com/api/v1/search and return parsed HNStory records.

    Raises on HTTP error / network failure / parse failure — the tool
    wrapper translates those into a tool-result error string.
    """
    url = _build_url(query, n, kind, time_window, min_points)
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset("utf-8")
        text = raw.decode(encoding, errors="replace")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return []
    return _parse_hits(payload)


def format_results(
    stories: list[HNStory], query: str
) -> str:
    """Render a list of HNStory as a markdown numbered list."""
    if not stories:
        return f"<no hn results for {query!r}>"
    lines: list[str] = [f"# Hacker News results for {query!r}", ""]
    for i, s in enumerate(stories, 1):
        title = s.title or "(no title)"
        meta_bits: list[str] = []
        if s.author:
            meta_bits.append(f"by {s.author}")
        meta_bits.append(f"{s.points} pts")
        meta_bits.append(f"{s.num_comments} comments")
        if s.created_at:
            # YYYY-MM-DDTHH:MM:SS.000Z — keep just the date for terseness
            meta_bits.append(s.created_at[:10])
        meta = " · ".join(meta_bits)
        lines.append(f"{i}. **{title}** — {s.permalink}")
        lines.append(f"   {meta}")
        if s.url and s.url != s.permalink:
            lines.append(f"   link: {s.url}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def register(api):
    for warning in _config_warnings(api.plugin_config or {}):
        api.log("warning", warning)

    def hn_search(
        query: str,
        n: int = 10,
        kind: str = "story",
        time_window: str = "all",
        min_points: int | None = None,
    ):
        """Search Hacker News via Algolia.

        Use for technical-consensus questions where HN discussions
        out-signal generic web results — "what do people think of
        <library>", "how is <startup> received", "real-world
        experience with <technology>". Algolia indexes everything:
        stories, comments, Ask HN, Show HN.

        On a successful (non-empty) search, returns an Attachment:
        the markdown summary rides inline and the structured JSON
        list of items is saved to attachments. The footer points at
        the JSON; downstream tools can consume the structured form
        directly. Set ``[plugins.hn-search] save_structured = false``
        to revert to a markdown-only string return.

        Args:
            query: Search query. Plain text.
            n: Number of results (default 10, max 50).
            kind: One of ``story`` (default), ``comment``, ``poll``,
                ``any``. ``any`` removes the type filter.
            time_window: One of ``hour``, ``day``, ``week``, ``month``,
                ``year``, ``all`` (default). Restricts to items
                created in that window.
            min_points: Optional minimum-score filter. Only items
                with at least this many points come back. Useful
                for cutting noise on broad queries.

        Returns:
            On success: Attachment(inline_text=<markdown summary>,
                content=<JSON list of items>, suffix=".json").
            On miss: ``<no hn results for ...>`` string marker.
            On failure: ``<hn-search error: ...>`` string marker
                (HTTP error, parse error, etc.).
        """
        if not query or not query.strip():
            return "<query is empty>"
        try:
            n_int = int(n)
        except (TypeError, ValueError):
            return f"<error: n must be an integer, got {n!r}>"
        if n_int < 1:
            return "<error: n must be >= 1>"
        if n_int > _MAX_RESULTS:
            n_int = _MAX_RESULTS
        if kind not in _VALID_KINDS:
            return (
                f"<error: kind must be one of {sorted(_VALID_KINDS)}, "
                f"got {kind!r}>"
            )
        if time_window not in _VALID_TIME_WINDOWS:
            return (
                f"<error: time_window must be one of "
                f"{sorted(_VALID_TIME_WINDOWS)}, got {time_window!r}>"
            )
        if min_points is not None:
            try:
                min_points_int: int | None = int(min_points)
            except (TypeError, ValueError):
                return (
                    f"<error: min_points must be an integer, got "
                    f"{min_points!r}>"
                )
            if min_points_int < 0:
                return (
                    f"<error: min_points must be >= 0, got "
                    f"{min_points_int}>"
                )
        else:
            min_points_int = None

        cfg = api.plugin_config or {}
        timeout_s = _resolve_timeout(cfg)
        save_structured = _resolve_save_structured(cfg)

        try:
            stories = hn_text_search(
                query,
                n=n_int,
                kind=kind,
                time_window=time_window,
                min_points=min_points_int,
                timeout_s=timeout_s,
            )
        except urllib.error.HTTPError as e:
            return f"<hn-search error: HTTP {e.code}: {e.reason}>"
        except urllib.error.URLError as e:
            return f"<hn-search error: network failure: {e.reason}>"
        except Exception as e:
            return f"<hn-search error: {e}>"

        markdown = format_results(stories, query)
        if not save_structured or not stories:
            return markdown

        structured = json.dumps(
            [
                {
                    "title": s.title,
                    "url": s.url,
                    "permalink": s.permalink,
                    "author": s.author,
                    "points": s.points,
                    "num_comments": s.num_comments,
                    "created_at": s.created_at,
                    "object_id": s.object_id,
                    "type": s.type,
                }
                for s in stories
            ],
            indent=2,
            ensure_ascii=False,
        )
        return Attachment(
            content=structured,
            inline_text=markdown,
            suffix=".json",
        )

    # Role-only: keeps hn_search out of the root agent's schema.
    # Allowlisted in the bundled researcher role; reach for it via
    # `pyagent --role researcher` or spawn_subagent.
    api.register_tool("hn_search", hn_search, role_only=True)
