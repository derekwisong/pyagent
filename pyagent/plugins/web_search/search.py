"""DuckDuckGo search backend.

Two entrypoints, both pure functions returning structured data so the
plugin's tool wrappers can render them however they like (and tests can
mock the network at one well-defined seam):

  - `ddg_text_search(query, n)`  — list-style results (title, url,
    snippet) via the `ddgs` library, which scrapes the DDG HTML
    endpoint. Lifted from the user-scope `web-search` skill.
  - `ddg_instant_answer(query)`  — the JSON instant-answer API at
    api.duckduckgo.com. Returns a parsed dict with the abstract,
    direct answer, definition, and related topics.

The seam is intentionally narrow: `ddg_text_search` returns a list of
`SearchResult` and `ddg_instant_answer` returns an `InstantAnswer` —
both plain dataclasses. Tool framing (markdown formatting, related-
topic toggle, etc.) lives in __init__.py.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


_INSTANT_ANSWER_URL = "https://api.duckduckgo.com/"
_USER_AGENT = "Mozilla/5.0 (compatible; pyagent-web-search)"
_INSTANT_TIMEOUT = 10


# Retry policy defaults. These are overridable via [plugins.web-search]
# in config.toml — see web_search/__init__.py for the resolver.
_DEFAULT_ATTEMPTS = 3
_DEFAULT_BACKOFF_S: tuple[float, ...] = (1.0, 3.0)
_DEFAULT_BACKEND = "auto"


class SearchRateLimited(Exception):
    """Raised when the upstream signals rate-limiting. Don't retry —
    the agent should pause and / or change strategy."""


class SearchBackoffExhausted(Exception):
    """Raised when every retry attempt failed with retryable errors.
    Distinct from a generic ``<search error: ...>`` so the agent can
    reason about the failure mode (transient backend trouble vs.
    programmer error)."""


@dataclass(frozen=True)
class SearchResult:
    """One DuckDuckGo search result."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class InstantAnswer:
    """Parsed payload from the DuckDuckGo instant-answer API.

    Most fields are empty for most queries — DDG's instant-answer
    coverage is narrow (definitions, calculations, well-known
    entities). Callers should pick the first non-empty field in
    preference order: `answer` → `abstract` → `definition`. If all
    three are empty, fall back to `related` or to `web_search`.
    """

    answer: str = ""
    abstract: str = ""
    abstract_source: str = ""
    abstract_url: str = ""
    definition: str = ""
    definition_source: str = ""
    definition_url: str = ""
    heading: str = ""
    related: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def ddg_text_search(
    query: str,
    n: int = 10,
    *,
    attempts: int = _DEFAULT_ATTEMPTS,
    backoff_s: Sequence[float] = _DEFAULT_BACKOFF_S,
    backend: str = _DEFAULT_BACKEND,
) -> list[SearchResult]:
    """Run a DDG text search with retry/backoff; return up to `n` results.

    Uses the `ddgs` library, which fans out across ~10 backend engines
    (DuckDuckGo, Bing, Brave, Google, Yandex, Yahoo, Mojeek,
    Wikipedia, Grokipedia) in a parallel ThreadPoolExecutor and
    aggregates results. The library only raises when *every* engine
    failed or returned empty. Each attempt re-shuffles the engine
    order, so a retry effectively samples a different subset of
    engines.

    Retry behavior:
      - ``RatelimitException`` → no retry. Raises ``SearchRateLimited``
        immediately so the caller can surface a distinct marker.
      - ``TimeoutException`` / generic ``DDGSException`` → retry with
        backoff. After ``attempts-1`` failures, raises
        ``SearchBackoffExhausted`` carrying the last error.
      - Any other exception (programmer error, ImportError, etc.)
        propagates as today — caller's catch-all handles them.

    Args:
        query: Search query.
        n: Number of results (max applied upstream by caller).
        attempts: Total attempts including the initial. ``1`` disables
            retry. Defaults to 3 (initial + 2 retries).
        backoff_s: Per-retry sleep durations. ``backoff_s[i]`` is the
            sleep before attempt ``i+1`` (the (i+1)-th retry). Length
            is expected to be ``attempts-1``; if shorter, the last
            value is reused for any subsequent retries.
        backend: Comma-delimited engine names or ``"auto"`` for the
            full set. Defaults to ``"auto"``.

    Returns:
        A list of SearchResult dataclasses. Empty list means the
        engines all returned no results (a successful empty result;
        not a retried failure).
    """
    # Local import so an environment missing `ddgs` still loads the
    # plugin module (the tool will return a clean error when called).
    from ddgs import DDGS
    from ddgs.exceptions import (
        DDGSException,
        RatelimitException,
        TimeoutException,
    )

    if attempts < 1:
        attempts = 1

    last_err: Exception | None = None
    for i in range(attempts):
        try:
            results = DDGS().text(query, max_results=n, backend=backend)
        except RatelimitException as e:
            # Don't retry — the upstream is explicitly throttling.
            # The caller surfaces a distinct marker so the agent can
            # back off rather than re-fire the same query.
            raise SearchRateLimited(str(e)) from e
        except (TimeoutException, DDGSException) as e:
            last_err = e
            logger.info(
                "web_search attempt %d/%d failed: %s", i + 1, attempts, e
            )
            if i < attempts - 1:
                # Sleep before the next attempt. backoff_s shorter
                # than attempts-1 reuses the last value.
                if backoff_s:
                    delay = backoff_s[min(i, len(backoff_s) - 1)]
                else:
                    delay = 0.0
                if delay > 0:
                    time.sleep(delay)
            continue
        else:
            out: list[SearchResult] = []
            for r in results or []:
                out.append(
                    SearchResult(
                        title=str(r.get("title") or "").strip(),
                        url=str(r.get("href") or "").strip(),
                        snippet=str(r.get("body") or "").strip(),
                    )
                )
            if not out:
                # Empty result with no exception is the silent-break
                # signature — could be a genuine niche query or scraper
                # drift after a DDG HTML change. The agent gets the
                # `<no results>` marker either way; the warning lets
                # an operator notice the pattern in logs (and tells
                # them to check whether ddgs needs an update).
                logger.warning(
                    "web_search: backend %r returned 0 results for "
                    "%r — may be a niche query or scraper drift",
                    backend,
                    query,
                )
            return out

    # All attempts exhausted. last_err is set because we only land
    # here if the loop body raised on every iteration.
    raise SearchBackoffExhausted(
        f"after {attempts} attempt(s): {last_err}"
    )


def _parse_instant_payload(data: dict) -> InstantAnswer:
    """Pull the small set of fields we care about out of the DDG
    instant-answer JSON. Tolerant of missing keys — DDG omits anything
    it doesn't have for a given query."""
    related_raw = data.get("RelatedTopics") or []
    related: list[tuple[str, str]] = []
    for entry in related_raw:
        if not isinstance(entry, dict):
            continue
        # Top-level entries are either {Text, FirstURL} or
        # {Name, Topics: [...]} groupings. We flatten one level.
        if "Topics" in entry and isinstance(entry["Topics"], list):
            for sub in entry["Topics"]:
                if isinstance(sub, dict):
                    text = str(sub.get("Text") or "").strip()
                    url = str(sub.get("FirstURL") or "").strip()
                    if text and url:
                        related.append((text, url))
        else:
            text = str(entry.get("Text") or "").strip()
            url = str(entry.get("FirstURL") or "").strip()
            if text and url:
                related.append((text, url))

    return InstantAnswer(
        answer=str(data.get("Answer") or "").strip(),
        abstract=str(data.get("AbstractText") or "").strip(),
        abstract_source=str(data.get("AbstractSource") or "").strip(),
        abstract_url=str(data.get("AbstractURL") or "").strip(),
        definition=str(data.get("Definition") or "").strip(),
        definition_source=str(data.get("DefinitionSource") or "").strip(),
        definition_url=str(data.get("DefinitionURL") or "").strip(),
        heading=str(data.get("Heading") or "").strip(),
        related=tuple(related),
    )


def ddg_instant_answer(query: str) -> InstantAnswer:
    """Hit the DDG instant-answer JSON API.

    May raise on network/HTTP failure or JSON-decode failure — the
    tool wrapper translates those into a tool-result error string.
    """
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "no_html": "1",
            "no_redirect": "1",
            "skip_disambig": "1",
        }
    )
    url = f"{_INSTANT_ANSWER_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_INSTANT_TIMEOUT) as resp:
        raw = resp.read()
        encoding = resp.headers.get_content_charset("utf-8")
        text = raw.decode(encoding, errors="replace")
    data = json.loads(text)
    if not isinstance(data, dict):
        return InstantAnswer()
    return _parse_instant_payload(data)


def format_search_results(
    results: list[SearchResult], query: str
) -> str:
    """Render a list of `SearchResult` as a markdown numbered list.

    Empty input yields a `<no results ...>` marker so the agent can
    distinguish "search ran, found nothing" from a tool error.
    """
    if not results:
        return f"<no results for {query!r}>"
    lines: list[str] = [f"# Search results for {query!r}", ""]
    for i, r in enumerate(results, 1):
        title = r.title or "(no title)"
        lines.append(f"{i}. **{title}** — {r.url}")
        if r.snippet:
            lines.append(f"   {r.snippet}")
        lines.append("")
    # Drop a trailing blank line for cleanliness.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def format_instant_answer(
    ans: InstantAnswer, query: str, related: bool = False
) -> str:
    """Render an `InstantAnswer` for the agent.

    Picks the highest-signal field available; appends related-topic
    links only when `related=True`. Returns a `<no instant answer>`
    marker when DDG had nothing — most queries fall here, and the
    marker is the agent's cue to fall back to `web_search`.
    """
    primary = ""
    if ans.answer:
        primary = ans.answer
    elif ans.abstract:
        src = (
            f" (source: {ans.abstract_source})"
            if ans.abstract_source
            else ""
        )
        primary = f"{ans.abstract}{src}"
    elif ans.definition:
        src = (
            f" (definition: {ans.definition_source})"
            if ans.definition_source
            else ""
        )
        primary = f"{ans.definition}{src}"

    if not primary and not (related and ans.related):
        return f"<no instant answer for {query!r}>"

    parts: list[str] = []
    if ans.heading and primary:
        parts.append(f"**{ans.heading}**")
    if primary:
        parts.append(primary)

    if related and ans.related:
        if parts:
            parts.append("")
        parts.append("Related:")
        for text, url in ans.related[:10]:
            parts.append(f"- {text} — {url}")

    return "\n".join(parts).strip()
