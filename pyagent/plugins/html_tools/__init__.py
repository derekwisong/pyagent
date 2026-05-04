"""html-tools — bundled plugin for CSS-selecting against HTML files.

One tool, `html_select`, operating on local HTML files (typically
session attachments produced by `fetch_url`). The conversion code
lives in `extraction.py` and is also imported by core's `fetch_url`
so the default-clean fetch path and the agent-facing tool share one
implementation. Role-only — allowlisted in the bundled researcher
role; the working agent gets HTML→markdown for free via fetch_url's
inline rendering.
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

    def html_select(path: str, css: str, limit: int = 50) -> str:
        """Run a CSS selector against an HTML file; return matches as
        markdown.

        The structured-extraction escape hatch when fetch_url's
        flattened markdown lost the shape you care about — table rows,
        list items at a specific selector, links inside a sidebar.
        Each match is rendered as a markdown block separated by `---`.

        Args:
            path: Path to the HTML file.
            css: CSS selector. Examples: `"table.wikitable tr"`,
                `"div.article a[href]"`, `"h2"`.
            limit: Maximum matches to return (default 50).

        Returns:
            Markdown of the matched elements joined by horizontal
            rules. Trailing note when more matches exist than `limit`.
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

    # Role-only: html_select is the structured-extraction escape
    # hatch when fetch_url's inline markdown lost a specific shape.
    # Allowlisted in the bundled researcher role; the working agent
    # rarely needs CSS-selector-level extraction.
    api.register_tool("html_select", html_select, role_only=True)
