"""html-tools — bundled plugin for interrogating HTML attachments.

Two tools, both operating on local HTML files (typically session
attachments produced by `fetch_url`). The conversion code lives in
`extraction.py` and is also imported by core's `fetch_url` so the
default-clean fetch path and the agent-facing tools share one
implementation.
"""

from __future__ import annotations

from pathlib import Path

from pyagent import permissions
from pyagent.plugins.html_tools import extraction


def register(api):
    def _read_html(path: str) -> tuple[bool, str]:
        """Returns (ok, payload). On error, ok=False and payload is a
        leading-`<>`-marker error string the tool returns directly."""
        if not permissions.require_access(path):
            return (
                False,
                f"<permission denied (outside workspace): {path}>",
            )
        p = Path(path)
        try:
            return (True, p.read_text())
        except FileNotFoundError:
            return (False, f"<file not found: {path}>")
        except IsADirectoryError:
            return (False, f"<is a directory, not a file: {path}>")
        except PermissionError:
            return (False, f"<permission denied: {path}>")
        except UnicodeDecodeError:
            return (
                False,
                f"<cannot decode {path} as UTF-8 — not an HTML file?>",
            )

    def html_to_md(path: str, main_content: bool = True) -> str:
        """Render an HTML file as markdown.

        Reach for this on a saved attachment from `fetch_url` (or any
        local `.html` file) when you want clean, LLM-readable text:
        headings as `#`, lists as `-`, tables as pipes, links as
        `[text](url)`. Tables and structure survive — a flat tag-strip
        would lose them.

        Args:
            path: Path to the HTML file.
            main_content: If True (default), reduce to the article body
                first by stripping nav/aside/footer/header and preferring
                a `<main>` / `<article>` / known content wrapper. Set
                False for reference pages (Wikipedia, docs) where the
                whole document is the content.

        Returns:
            Markdown text. Large outputs auto-offload via the standard
            attachment path.
        """
        ok, payload = _read_html(path)
        if not ok:
            return payload
        return extraction.html_to_markdown(payload, main_content=main_content)

    def html_select(path: str, css: str, limit: int = 50) -> str:
        """Run a CSS selector against an HTML file; return matches as
        markdown.

        The structured-extraction escape hatch when `html_to_md` would
        lose the shape you care about — table rows, list items at a
        specific selector, links inside a sidebar, etc. Each match is
        rendered as a markdown block separated by `---`.

        Args:
            path: Path to the HTML file.
            css: CSS selector. Examples: `"table.wikitable tr"`,
                `"div.article a[href]"`, `"h2"`.
            limit: Maximum matches to return. Defaults to 50; raise it
                deliberately when you know the page has many small
                matches and you want them all.

        Returns:
            Markdown of the matched elements, joined by horizontal
            rules. A trailing note mentions the truncation if more
            matches exist than `limit`.
        """
        ok, payload = _read_html(path)
        if not ok:
            return payload
        md, total, returned = extraction.html_select_to_markdown(
            payload, css, limit=limit
        )
        if total == 0:
            return f"<no matches for selector {css!r} in {path}>"
        if returned < total:
            md = (
                md
                + f"\n[matched {total}; showing first {returned}. "
                f"Re-run with a tighter selector or higher `limit` to see more.]\n"
            )
        return md

    api.register_tool("html_to_md", html_to_md)
    api.register_tool("html_select", html_select)
