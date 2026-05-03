"""reddit-search — bundled plugin: search Reddit via the public
reddit.com/search.json endpoint.

One tool, ``reddit_search(query, n=10, subreddit=None,
time_window="all", sort="relevance")``. Returns an Attachment whose
``inline_text`` is a human-readable markdown summary and whose
``content`` is the structured JSON list of posts (title, url, score,
comments, age, author, subreddit). Same shape as ``web_search`` post-
#91 — markdown rides inline, structured form lives in attachments
for chaining via ``extract_doc`` or future tool composition.

Why a separate plugin (not a search-source slot in ``web_search``):
Reddit results have a distinct shape (score, num_comments, subreddit)
and distinct query knobs (sub-restriction, time window) that don't
fit ``web_search``'s (query, n) signature. The metasearch
under-surfaces Reddit content for "real-world Q&A" queries, which is
the gap this fills.

Configuration (all optional, all read at call time):

  ``[plugins.reddit-search]`` table in pyagent's config TOML
    ``timeout_s`` — HTTP timeout per attempt. Default 10.
    ``user_agent`` — User-Agent header sent to Reddit. Default
        identifies pyagent. Reddit gates anonymous traffic on the
        UA — leaving it as a generic Python UA earns a 429.
    ``save_structured`` — when true (default), save the JSON list
        as an attachment alongside the markdown. Set to false for
        a legacy markdown-only string return.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pyagent.session import Attachment

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 10
_DEFAULT_USER_AGENT = (
    "pyagent-reddit-search/0.1 (+https://github.com/derekwisong/pyagent)"
)
_DEFAULT_SAVE_STRUCTURED = True
_MAX_RESULTS = 25  # Reddit caps at 100 per page; we cap lower to keep results focused
_VALID_TIME_WINDOWS = {"hour", "day", "week", "month", "year", "all"}
_VALID_SORTS = {"relevance", "hot", "top", "new", "comments"}


@dataclass(frozen=True)
class RedditPost:
    """One Reddit search result, normalized."""

    title: str
    url: str          # external URL the post links to (or self-permalink for text posts)
    permalink: str    # reddit.com permalink — always present
    subreddit: str
    author: str
    score: int
    num_comments: int
    created_utc: float
    selftext_excerpt: str   # first ~300 chars of self-post body, "" for link posts


def _resolve_timeout(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("timeout_s")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _DEFAULT_TIMEOUT_S


def _resolve_user_agent(plugin_cfg: dict) -> str:
    raw = plugin_cfg.get("user_agent")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _DEFAULT_USER_AGENT


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
    if "user_agent" in plugin_cfg:
        raw = plugin_cfg["user_agent"]
        if not isinstance(raw, str) or not raw.strip():
            out.append(
                f"user_agent must be a non-empty string — using default"
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
    subreddit: str | None,
    time_window: str,
    sort: str,
) -> str:
    base = (
        f"https://www.reddit.com/r/{urllib.parse.quote(subreddit)}/search.json"
        if subreddit
        else "https://www.reddit.com/search.json"
    )
    params: dict[str, str] = {
        "q": query,
        "limit": str(n),
        "t": time_window,
        "sort": sort,
        "raw_json": "1",
    }
    if subreddit:
        # Restrict to that sub specifically; without this, Reddit's
        # /r/<sub>/search.json silently spans all of Reddit on some
        # paths.
        params["restrict_sr"] = "1"
    return f"{base}?{urllib.parse.urlencode(params)}"


def _parse_listing(payload: dict) -> list[RedditPost]:
    children = (payload.get("data") or {}).get("children") or []
    out: list[RedditPost] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        d = child.get("data") or {}
        if not isinstance(d, dict):
            continue
        permalink_path = str(d.get("permalink") or "")
        permalink = (
            f"https://www.reddit.com{permalink_path}"
            if permalink_path.startswith("/")
            else permalink_path
        )
        external_url = str(d.get("url") or permalink).strip()
        selftext = str(d.get("selftext") or "").strip()
        excerpt = (
            (selftext[:297] + "...") if len(selftext) > 300 else selftext
        )
        try:
            score = int(d.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        try:
            num_comments = int(d.get("num_comments") or 0)
        except (TypeError, ValueError):
            num_comments = 0
        try:
            created_utc = float(d.get("created_utc") or 0)
        except (TypeError, ValueError):
            created_utc = 0.0
        out.append(
            RedditPost(
                title=str(d.get("title") or "").strip(),
                url=external_url,
                permalink=permalink,
                subreddit=str(d.get("subreddit") or "").strip(),
                author=str(d.get("author") or "").strip(),
                score=score,
                num_comments=num_comments,
                created_utc=created_utc,
                selftext_excerpt=excerpt,
            )
        )
    return out


def reddit_text_search(
    query: str,
    *,
    n: int = 10,
    subreddit: str | None = None,
    time_window: str = "all",
    sort: str = "relevance",
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> list[RedditPost]:
    """Hit reddit.com/search.json and return parsed RedditPost records.

    Raises on HTTP error / network failure / parse failure — the tool
    wrapper translates those into a tool-result error string.
    """
    url = _build_url(query, n, subreddit, time_window, sort)
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset("utf-8")
        text = raw.decode(encoding, errors="replace")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        return []
    return _parse_listing(payload)


def format_results(
    posts: list[RedditPost], query: str, subreddit: str | None
) -> str:
    """Render a list of RedditPost as a markdown numbered list."""
    if not posts:
        scope = f" in r/{subreddit}" if subreddit else ""
        return f"<no reddit results for {query!r}{scope}>"
    scope = f" in r/{subreddit}" if subreddit else ""
    lines: list[str] = [f"# Reddit results for {query!r}{scope}", ""]
    for i, p in enumerate(posts, 1):
        title = p.title or "(no title)"
        meta_bits = [f"r/{p.subreddit}"] if p.subreddit else []
        if p.author:
            meta_bits.append(f"u/{p.author}")
        meta_bits.append(f"{p.score} pts")
        meta_bits.append(f"{p.num_comments} comments")
        meta = " · ".join(meta_bits)
        lines.append(f"{i}. **{title}** — {p.permalink}")
        lines.append(f"   {meta}")
        if p.url and p.url != p.permalink:
            lines.append(f"   link: {p.url}")
        if p.selftext_excerpt:
            lines.append(f"   {p.selftext_excerpt}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def register(api):
    for warning in _config_warnings(api.plugin_config or {}):
        api.log("warning", warning)

    def reddit_search(
        query: str,
        n: int = 10,
        subreddit: str | None = None,
        time_window: str = "all",
        sort: str = "relevance",
    ):
        """Search Reddit posts via reddit.com/search.json.

        Use for real-world Q&A, community discussion, and "what do
        people actually think about X" queries that the metasearch
        in ``web_search`` covers shallowly. Reddit search is the
        right surface when the answer lives inside conversations,
        not on landing pages.

        On a successful (non-empty) search, returns an Attachment:
        the markdown summary rides inline and the structured JSON
        list of posts is saved to attachments. The footer points at
        the JSON; downstream tools (``extract_doc`` etc.) can consume
        the structured list directly without re-running the search.
        Set ``[plugins.reddit-search] save_structured = false`` to
        revert to a markdown-only string return.

        Args:
            query: Search query. Plain text. Reddit's search supports
                operators like ``site:`` and ``self:yes`` — pass them
                through as part of ``query``.
            n: Number of results (default 10, max 25).
            subreddit: Optional subreddit name (without the ``r/``
                prefix). When set, the search is restricted to that
                sub. Without it, the search spans all of Reddit.
            time_window: One of ``hour``, ``day``, ``week``, ``month``,
                ``year``, ``all`` (default).
            sort: One of ``relevance`` (default), ``hot``, ``top``,
                ``new``, ``comments``.

        Returns:
            On success: Attachment(inline_text=<markdown summary>,
                content=<JSON list of posts>, suffix=".json").
            On miss: ``<no reddit results for ...>`` string marker.
            On failure: ``<reddit-search error: ...>`` string marker
                (HTTP error, parse error, rate limit, etc.).
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
        if subreddit is not None:
            if not isinstance(subreddit, str) or not subreddit.strip():
                return (
                    f"<error: subreddit must be a non-empty string when "
                    f"set, got {subreddit!r}>"
                )
            subreddit = subreddit.strip().lstrip("/").removeprefix("r/")
        if time_window not in _VALID_TIME_WINDOWS:
            return (
                f"<error: time_window must be one of "
                f"{sorted(_VALID_TIME_WINDOWS)}, got {time_window!r}>"
            )
        if sort not in _VALID_SORTS:
            return (
                f"<error: sort must be one of {sorted(_VALID_SORTS)}, "
                f"got {sort!r}>"
            )

        cfg = api.plugin_config or {}
        timeout_s = _resolve_timeout(cfg)
        user_agent = _resolve_user_agent(cfg)
        save_structured = _resolve_save_structured(cfg)

        try:
            posts = reddit_text_search(
                query,
                n=n_int,
                subreddit=subreddit,
                time_window=time_window,
                sort=sort,
                timeout_s=timeout_s,
                user_agent=user_agent,
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                return (
                    f"<reddit-search error: rate limited (HTTP 429); "
                    f"pause and try again later — set a more "
                    f"identifying user_agent in config if persistent>"
                )
            return f"<reddit-search error: HTTP {e.code}: {e.reason}>"
        except urllib.error.URLError as e:
            return f"<reddit-search error: network failure: {e.reason}>"
        except Exception as e:
            return f"<reddit-search error: {e}>"

        markdown = format_results(posts, query, subreddit)
        if not save_structured or not posts:
            return markdown

        structured = json.dumps(
            [
                {
                    "title": p.title,
                    "url": p.url,
                    "permalink": p.permalink,
                    "subreddit": p.subreddit,
                    "author": p.author,
                    "score": p.score,
                    "num_comments": p.num_comments,
                    "created_utc": p.created_utc,
                    "selftext_excerpt": p.selftext_excerpt,
                }
                for p in posts
            ],
            indent=2,
            ensure_ascii=False,
        )
        return Attachment(
            content=structured,
            inline_text=markdown,
            suffix=".json",
        )

    api.register_tool("reddit_search", reddit_search)
