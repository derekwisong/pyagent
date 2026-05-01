"""web-search — bundled plugin for DuckDuckGo-backed web search.

Two tools:

  - `web_search(query, n=10)` — list-style search. Returns a markdown
    numbered list of results. Returned as a plain string; the agent's
    standard auto-offload threshold (8K chars) takes over for runaway
    `n` values, so a 50-result call won't bloat the conversation log.
  - `web_search_instant(query, related=False)` — DDG instant-answer
    API. Short string reply by definition.

Tool framing pushes the model toward a search-then-fetch loop rather
than search-as-default — most queries the model thinks "I should
search" can be answered from training data, and a network round trip
costs the user real time and money.

Lifted from the user-scope `web-search` skill at
~/.config/pyagent/skills/web-search/. The skill is left in place so
older installs still work; the plugin shadows it in the catalog
because plugin tools call directly while skill scripts go through
`read_skill` + `execute`.
"""

from __future__ import annotations

from pyagent.plugins.web_search import search as _search


# Maximum results the agent is allowed to ask for in one call. DDG
# starts rate-limiting around the high 20s in practice; cap is a
# safety belt rather than the everyday limit.
_MAX_RESULTS = 25


def register(api):
    def web_search(query: str, n: int = 10) -> str:
        """Search the web via DuckDuckGo; return up to `n` results.

        Reach for this when `fetch_url` alone isn't enough — i.e. you
        don't have a URL yet, or you need to compare multiple sources.
        Most factual questions are better answered from training data;
        a search burns a network round trip and adds latency. When you
        do search, treat the result list as a *menu*: pair with
        `fetch_url` on the most promising URL rather than reading every
        snippet.

        Args:
            query: Search query. Plain text; DDG handles quoting and
                operators.
            n: Number of results (default 10, max 25). Higher values
                may auto-offload as an attachment.

        Returns:
            A markdown numbered list of `title — url` lines with
            snippets. `<no results ...>` if DDG returned nothing;
            `<search error: ...>` on network/parser failure. Large
            outputs auto-offload via the standard tool-result path.
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
        try:
            results = _search.ddg_text_search(query, n=n_int)
        except ImportError:
            return (
                "<search error: ddgs package not installed — "
                "run: pip install ddgs>"
            )
        except Exception as e:
            return f"<search error: {e}>"
        return _search.format_search_results(results, query)

    def web_search_instant(query: str, related: bool = False) -> str:
        """Hit DuckDuckGo's instant-answer API for a short factual reply.

        Use for definitions, well-known entities, and quick lookups
        where a list of links would be overkill. Coverage is narrow:
        most queries return `<no instant answer ...>` — that's the
        cue to fall back to `web_search` (or to answer from training
        data without searching at all).

        Args:
            query: Search query.
            related: If True, append up to 10 related-topic links.
                Default False — keeps the reply terse.

        Returns:
            A short markdown reply, or `<no instant answer ...>` /
            `<instant-answer error: ...>` on miss/failure.
        """
        if not query or not query.strip():
            return "<query is empty>"
        try:
            ans = _search.ddg_instant_answer(query)
        except Exception as e:
            return f"<instant-answer error: {e}>"
        return _search.format_instant_answer(ans, query, related=related)

    api.register_tool("web_search", web_search)
    api.register_tool("web_search_instant", web_search_instant)
