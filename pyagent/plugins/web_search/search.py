"""DuckDuckGo search backend.

`ddg_text_search(query, n)` returns list-style results (title, url,
snippet) via the `ddgs` library, which scrapes the DDG HTML endpoint.
Pure function returning structured data; tool framing (markdown
formatting, attachment side-save) lives in __init__.py.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)


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


