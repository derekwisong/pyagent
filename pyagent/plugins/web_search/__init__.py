"""web-search — bundled plugin for DuckDuckGo-backed web search.

One tool:

  - `web_search(query, n=10)` — list-style search. Returns a markdown
    numbered list of results plus a saved JSON attachment with the
    structured `[{title, url, snippet}]` form.

Tool framing pushes the model toward a search-then-fetch loop rather
than search-as-default — most queries the model thinks "I should
search" can be answered from training data, and a network round trip
costs the user real time and money.

Configuration (all optional, all read at call time):

  ``[plugins.web-search]`` table in pyagent's config TOML
    ``retry_attempts`` — total attempts including the initial.
        Defaults to 3 (initial + 2 retries). Set to 1 to disable
        retry. The metasearch backend reshuffles engine order each
        call, so a retry samples a different engine subset.
    ``retry_backoff_s`` — list of per-retry sleep durations.
        Defaults to ``[1.0, 3.0]`` for the standard 3-attempt run.
        Length is expected to match ``retry_attempts - 1``; if
        shorter, the last value is reused.
    ``backend`` — comma-delimited engine names or ``"auto"`` for
        the full set. Defaults to ``"auto"``. Use to pin to a known
        subset (e.g. ``"duckduckgo,brave,yahoo,mojeek"``) if a
        particular engine stays flaky.
    ``save_structured`` — when true (default), web_search saves the
        structured ``[{title, url, snippet}]`` list as a JSON
        attachment alongside the inline markdown. The footer points
        at the attachment so downstream tools (extract_doc, future
        chained workflows) can consume the URL list without
        re-running the search. Set to false to keep the legacy
        markdown-only string return.

Lifted from the user-scope `web-search` skill at
~/.config/pyagent/skills/web-search/. The skill is left in place so
older installs still work; the plugin shadows it in the catalog
because plugin tools call directly while skill scripts go through
`read_skill` + `execute`.
"""

from __future__ import annotations

import json
from typing import Sequence

from pyagent.plugins.web_search import search as _search
from pyagent.session import Attachment


# Maximum results the agent is allowed to ask for in one call. DDG
# starts rate-limiting around the high 20s in practice; cap is a
# safety belt rather than the everyday limit.
_MAX_RESULTS = 25

# Side-save the structured SearchResult list to attachments by
# default. Cost is ~3KB per call; benefit is downstream tools can
# consume the URL list without re-running the search, and the agent
# has a recovery path if it forgets which URLs it saw. Configurable
# via [plugins.web-search] save_structured.
_DEFAULT_SAVE_STRUCTURED = True


def _resolve_attempts(plugin_cfg: dict) -> int:
    raw = plugin_cfg.get("retry_attempts")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return _search._DEFAULT_ATTEMPTS


def _resolve_backoff_s(plugin_cfg: dict) -> Sequence[float]:
    raw = plugin_cfg.get("retry_backoff_s")
    if isinstance(raw, list) and raw:
        clean: list[float] = []
        for v in raw:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                clean.append(float(v))
        if clean:
            return clean
    return _search._DEFAULT_BACKOFF_S


def _resolve_backend(plugin_cfg: dict) -> str:
    raw = plugin_cfg.get("backend")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _search._DEFAULT_BACKEND


def _resolve_save_structured(plugin_cfg: dict) -> bool:
    raw = plugin_cfg.get("save_structured")
    if isinstance(raw, bool):
        return raw
    return _DEFAULT_SAVE_STRUCTURED


def _config_warnings(plugin_cfg: dict) -> list[str]:
    """Sanity-check the [plugins.web-search] table at register time.

    Bogus values still fall through to defaults via the resolvers — this
    surfaces typos at startup so they don't sit silent. No network
    probes; the default backend "auto" is intentionally not validated
    against the live engine list since the ddgs library handles
    unknown-backend fallback internally.
    """
    out: list[str] = []

    if "retry_attempts" in plugin_cfg:
        raw = plugin_cfg["retry_attempts"]
        if not isinstance(raw, int) or isinstance(raw, bool):
            out.append(
                f"retry_attempts must be a positive integer, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_search._DEFAULT_ATTEMPTS}"
            )
        elif raw < 1:
            out.append(
                f"retry_attempts must be >= 1, got {raw} — using "
                f"default {_search._DEFAULT_ATTEMPTS}"
            )

    if "retry_backoff_s" in plugin_cfg:
        raw = plugin_cfg["retry_backoff_s"]
        if not isinstance(raw, list):
            out.append(
                f"retry_backoff_s must be a list of numbers, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{list(_search._DEFAULT_BACKOFF_S)}"
            )
        else:
            bad = [
                v for v in raw
                if not isinstance(v, (int, float))
                or isinstance(v, bool)
                or v < 0
            ]
            if bad:
                out.append(
                    f"retry_backoff_s contains invalid entries "
                    f"(must be non-negative numbers): {bad!r}"
                )

    if "backend" in plugin_cfg:
        raw = plugin_cfg["backend"]
        if not isinstance(raw, str):
            out.append(
                f"backend must be a string, got {type(raw).__name__}: "
                f"{raw!r} — using default {_search._DEFAULT_BACKEND!r}"
            )
        elif not raw.strip():
            out.append("backend is set but empty — using default 'auto'")

    if "save_structured" in plugin_cfg:
        raw = plugin_cfg["save_structured"]
        if not isinstance(raw, bool):
            out.append(
                f"save_structured must be a bool, got "
                f"{type(raw).__name__}: {raw!r} — using default "
                f"{_DEFAULT_SAVE_STRUCTURED}"
            )

    return out


def register(api):
    # Lightweight register-time validation of the [plugins.web-search]
    # table. Bogus values still fall through to defaults at call time
    # via the _resolve_* helpers — this is purely about surfacing
    # config typos at startup instead of letting them sit silent.
    for warning in _config_warnings(api.plugin_config or {}):
        api.log("warning", warning)

    def web_search(query: str, n: int = 10):
        """Search the web via DuckDuckGo; return up to `n` results.

        Treat results as a *menu*: pair with `fetch_url` on the most
        promising URL rather than reading every snippet.

        Args:
            query: Search query. Plain text; DDG handles quoting
                and operators.
            n: Number of results (default 10, max 25).

        Returns:
            Markdown numbered list of `title — url` + snippet. On
            success a `[{title, url, snippet}]` JSON attachment is
            also saved alongside; the inline markdown is the complete
            answer. Markers on miss/failure: `<no results ...>`,
            `<search error: rate limited; ...>` (back off),
            `<search error: backend unavailable ...>` (try a
            different query or fall back to fetch_url),
            `<search error: ...>` (other).
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

        cfg = api.plugin_config or {}
        attempts = _resolve_attempts(cfg)
        backoff_s = _resolve_backoff_s(cfg)
        backend = _resolve_backend(cfg)
        save_structured = _resolve_save_structured(cfg)

        try:
            results = _search.ddg_text_search(
                query,
                n=n_int,
                attempts=attempts,
                backoff_s=backoff_s,
                backend=backend,
            )
        except ImportError:
            return (
                "<search error: ddgs package not installed — "
                "run: pip install ddgs>"
            )
        except _search.SearchRateLimited as e:
            return (
                f"<search error: rate limited; pause and try again "
                f"later ({e})>"
            )
        except _search.SearchBackoffExhausted as e:
            return (
                f"<search error: backend unavailable {e}; try a "
                f"different query or use fetch_url with a known URL>"
            )
        except Exception as e:
            return f"<search error: {e}>"

        markdown = _search.format_search_results(results, query)
        # Empty-results path: format_search_results returned the
        # `<no results ...>` marker. Nothing structurally useful to
        # save; return the marker as a plain string so error/empty
        # behavior stays consistent.
        if not save_structured or not results:
            return markdown

        structured = json.dumps(
            [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
            indent=2,
            ensure_ascii=False,
        )
        return Attachment(
            content=structured,
            inline_text=markdown,
            suffix=".json",
        )

    # Role-only: keeps web_search out of the root agent's schema
    # list. Reach for it via `pyagent --role researcher` (or
    # spawn_subagent(role="researcher", ...)) — the bundled
    # researcher role's allowlist names it explicitly.
    api.register_tool("web_search", web_search, role_only=True)
