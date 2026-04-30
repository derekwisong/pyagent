"""HTML parsing and markdown rendering — shared between this plugin's
tools and core's `fetch_url`.

Lives inside the plugin package so disabling the plugin removes the
tool surface; core soft-imports from here, so when the plugin is
disabled, `fetch_url` falls back to raw-attachment-only and skips
markdown conversion. When the plugin is enabled (the default), both
the plugin tools and `fetch_url` share one implementation.
"""

from __future__ import annotations

from typing import Iterable

from bs4 import BeautifulSoup
from markdownify import markdownify


# Tags whose presence almost always means boilerplate, not content. We
# strip these unconditionally — even when main_content=False — because
# they break markdown rendering (script/style content embedded in the
# converter output is noise, not signal).
_ALWAYS_STRIP = ("script", "style", "noscript", "template")

# Tags that frame the page chrome rather than the content. Stripped
# only when `main_content=True`.
_BOILERPLATE = ("nav", "aside", "footer", "header", "form")

# Selectors we try in order to find the article body when the page
# doesn't use a single <main> or <article> wrapper.
_MAIN_CANDIDATES = (
    "main",
    "article",
    "[role='main']",
    "#content",
    "#main-content",
    ".main-content",
    ".article-body",
    ".post-content",
    ".entry-content",
)


def _strip_tags(soup: BeautifulSoup, names: Iterable[str]) -> None:
    for tag_name in names:
        for tag in soup.find_all(tag_name):
            tag.decompose()


def _find_main(soup: BeautifulSoup):
    """Best-effort article-body locator. Returns the matched element or
    None if no candidate selector hits."""
    for selector in _MAIN_CANDIDATES:
        match = soup.select_one(selector)
        if match is not None:
            return match
    return None


def html_to_markdown(html: str, *, main_content: bool = True) -> str:
    """Render `html` as markdown.

    With `main_content=True`, attempts a readability-style reduction
    first: remove obvious boilerplate (nav/aside/footer/etc.) and prefer
    a `<main>` / `<article>` / known content-wrapper region if one
    exists. With `main_content=False`, converts the whole document
    after stripping only `<script>` / `<style>` noise.

    Returns markdown text. Whitespace is normalized; the markdown is
    safe to embed in conversation history.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    _strip_tags(soup, _ALWAYS_STRIP)

    target = soup
    if main_content:
        _strip_tags(soup, _BOILERPLATE)
        main = _find_main(soup)
        if main is not None:
            target = main

    md = markdownify(str(target), heading_style="ATX")
    # Markdownify can leave long runs of blank lines from divs/spans;
    # collapse runs of >2 blank lines down to 2 so the markdown reads
    # cleanly without changing the structural meaning.
    lines = md.splitlines()
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line.strip() == "":
            blanks += 1
            if blanks > 2:
                continue
        else:
            blanks = 0
        out.append(line.rstrip())
    return "\n".join(out).strip() + "\n"


def html_select_to_markdown(
    html: str, css: str, *, limit: int = 50
) -> tuple[str, int, int]:
    """Run a CSS selector against `html` and render matches as markdown.

    Returns `(markdown, matched_count, returned_count)`. When matches
    exceed `limit`, the markdown is truncated to the first `limit`
    items and the caller can mention the gap in its result message.
    Each returned match is separated by a horizontal rule so the agent
    can scan record boundaries without ambiguity.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    _strip_tags(soup, _ALWAYS_STRIP)
    matches = soup.select(css)
    total = len(matches)
    kept = matches[:limit] if limit > 0 else matches
    rendered: list[str] = []
    for el in kept:
        chunk = markdownify(str(el), heading_style="ATX").strip()
        if chunk:
            rendered.append(chunk)
    return ("\n\n---\n\n".join(rendered) + "\n", total, len(kept))
