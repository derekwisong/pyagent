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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


_INSTANT_ANSWER_URL = "https://api.duckduckgo.com/"
_USER_AGENT = "Mozilla/5.0 (compatible; pyagent-web-search)"
_INSTANT_TIMEOUT = 10


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


def ddg_text_search(query: str, n: int = 10) -> list[SearchResult]:
    """Run a DDG text search and return up to `n` results.

    Uses the `ddgs` library (DuckDuckGo HTML endpoint scraper). May
    raise on network failure or parser breakage — callers translate
    those into a tool-result error string rather than letting them
    bubble to the agent loop.
    """
    # Local import so an environment missing `ddgs` still loads the
    # plugin module (the tool will return a clean error when called).
    from ddgs import DDGS

    results = DDGS().text(query, max_results=n)
    out: list[SearchResult] = []
    for r in results or []:
        out.append(
            SearchResult(
                title=str(r.get("title") or "").strip(),
                url=str(r.get("href") or "").strip(),
                snippet=str(r.get("body") or "").strip(),
            )
        )
    return out


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
