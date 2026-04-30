"""End-to-end smoke for the html-tools plugin and the restructured
fetch_url.

Three concerns:

  1. **Plugin loads, registers expected tools.** With "html-tools" in
     built_in_plugins_enabled, `discover()` and `load()` produce the
     two tools (`html_to_md`, `html_select`).

  2. **Conversion behaves.** `html_to_markdown` produces clean markdown
     (headings, lists, links survive); `html_to_markdown(...,
     main_content=True)` strips boilerplate (nav/footer); selector
     extraction returns matched markdown chunks.

  3. **fetch_url returns an Attachment with raw content saved + the
     right preview shape.** The preview carries inline markdown for
     `format="md"` and a content-type-aware stub for `format="void"` /
     non-HTML / plugin-disabled paths. The Attachment's content holds
     the full raw response so html_select / grep / read_file can run
     against the saved path.

Run with:

    .venv/bin/python -m tests.smoke_html_tools
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from pyagent import config as config_mod, paths as paths_mod, plugins, tools
from pyagent.plugins.html_tools import extraction
from pyagent.session import Attachment


_NEWS_HTML = """
<!doctype html>
<html><head><title>headline</title></head>
<body>
  <header><nav>home / about / contact</nav></header>
  <aside class="sidebar">related: a, b, c</aside>
  <main>
    <h1>The Real Story</h1>
    <p>First paragraph with a <a href="https://example.com">link</a>.</p>
    <ul><li>one</li><li>two</li></ul>
  </main>
  <footer>copyright 2026</footer>
</body></html>
"""

_TABLE_HTML = """
<!doctype html>
<html><body>
  <h1>Books</h1>
  <table class="wikitable">
    <tr><th>Title</th><th>Year</th></tr>
    <tr><td>The Gunslinger</td><td>1982</td></tr>
    <tr><td>The Drawing of the Three</td><td>1987</td></tr>
  </table>
</body></html>
"""


def _check_extraction_main_content() -> None:
    md = extraction.html_to_markdown(_NEWS_HTML, main_content=True)
    # Boilerplate stripped.
    assert "home / about / contact" not in md, md
    assert "related:" not in md, md
    assert "copyright 2026" not in md, md
    # Real content kept, structure preserved.
    assert "The Real Story" in md, md
    assert "[link](https://example.com)" in md, md
    # List survives as bullets.
    assert "- one" in md or "* one" in md, md
    print(f"✓ extraction.main_content drops boilerplate, keeps structure")


def _check_extraction_full_document() -> None:
    md = extraction.html_to_markdown(_NEWS_HTML, main_content=False)
    # With main_content=False, boilerplate stays.
    assert "home / about / contact" in md or "home" in md, md
    assert "The Real Story" in md, md
    print(f"✓ extraction.main_content=False preserves the whole document")


def _check_extraction_select_table() -> None:
    md, total, returned = extraction.html_select_to_markdown(
        _TABLE_HTML, "table.wikitable tr", limit=10
    )
    assert total == 3, total
    assert returned == 3, returned
    assert "The Gunslinger" in md, md
    assert "1982" in md, md
    assert "The Drawing of the Three" in md, md
    # Records separated by horizontal rules.
    assert md.count("---") >= 2, md
    print(f"✓ extraction.select returns matched markdown, {total} matches")


def _check_extraction_select_limit() -> None:
    md, total, returned = extraction.html_select_to_markdown(
        _TABLE_HTML, "tr", limit=2
    )
    assert total == 3, total
    assert returned == 2, returned
    print(f"✓ extraction.select honors limit (matched {total}, kept {returned})")


def _check_plugin_loads_under_default_config() -> None:
    """With the default config, html-tools is in built_in_plugins_enabled
    and load() exposes both tools."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-htmltools-"))
    # Point config_dir at an empty temp dir so user/project config can't
    # mask the bundled defaults (e.g. an existing user config that hasn't
    # added "html-tools" yet).
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
            assert "html-tools" in cfg["built_in_plugins_enabled"], (
                cfg["built_in_plugins_enabled"]
            )
            loaded = plugins.load()
            tool_names = set(loaded.tools().keys())
    assert "html_to_md" in tool_names, tool_names
    assert "html_select" in tool_names, tool_names
    print(f"✓ plugin loads by default; tools = {sorted(tool_names)}")


class _FakeResponse:
    def __init__(
        self,
        text: str,
        *,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.text = text
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}


def _check_fetch_url_md_default() -> None:
    """fetch_url returns an Attachment whose content is the raw body
    and whose preview includes the converted markdown."""
    with mock.patch.object(
        tools.requests, "get", return_value=_FakeResponse(_NEWS_HTML)
    ):
        result = tools.fetch_url("https://example.com/news")
    assert isinstance(result, Attachment), type(result)
    assert result.content == _NEWS_HTML, "raw content should be saved verbatim"
    assert result.suffix == ".html", result.suffix
    # Preview should mention the URL/status, the content type, and
    # contain the converted markdown.
    assert "https://example.com/news" in result.preview, result.preview
    assert "status 200" in result.preview, result.preview
    assert "text/html" in result.preview, result.preview
    assert "The Real Story" in result.preview, result.preview
    assert "[link](https://example.com)" in result.preview, result.preview
    # Boilerplate must not survive the main-content extraction.
    assert "home / about / contact" not in result.preview, result.preview
    print(f"✓ fetch_url(format='md') saves raw + inlines markdown")


def _check_fetch_url_void() -> None:
    """format='void' returns only the stub — no markdown body."""
    with mock.patch.object(
        tools.requests, "get", return_value=_FakeResponse(_NEWS_HTML)
    ):
        result = tools.fetch_url(
            "https://example.com/x", format="void"
        )
    assert isinstance(result, Attachment), type(result)
    assert result.content == _NEWS_HTML, "raw still saved"
    assert "format=\"void\"" in result.preview, result.preview
    # Markdown body must NOT appear in preview.
    assert "The Real Story" not in result.preview, result.preview
    assert "[link]" not in result.preview, result.preview
    print(f"✓ fetch_url(format='void') saves raw, omits markdown body")


def _check_fetch_url_non_html() -> None:
    """JSON / non-HTML responses skip conversion regardless of format."""
    body = '{"ok": true, "items": [1, 2, 3]}'
    with mock.patch.object(
        tools.requests,
        "get",
        return_value=_FakeResponse(
            body, content_type="application/json"
        ),
    ):
        result = tools.fetch_url("https://api.example.com/x")
    assert isinstance(result, Attachment), type(result)
    assert result.content == body, result.content
    assert result.suffix == ".json", result.suffix
    assert "application/json" in result.preview, result.preview
    assert "Non-HTML" in result.preview, result.preview
    print(f"✓ fetch_url skips conversion for non-HTML responses")


def _check_fetch_url_large_md_truncates() -> None:
    """When converted markdown exceeds the inline ceiling, the preview
    is truncated and points at html_to_md on the saved path."""
    big_html = (
        "<html><body><main>"
        + "".join(f"<p>line {i} " + "x " * 200 + "</p>" for i in range(60))
        + "</main></body></html>"
    )
    with mock.patch.object(
        tools.requests, "get", return_value=_FakeResponse(big_html)
    ):
        result = tools.fetch_url("https://example.com/big")
    assert isinstance(result, Attachment), type(result)
    assert "markdown truncated" in result.preview, result.preview
    assert "html_to_md" in result.preview, result.preview
    # Raw still saved untouched.
    assert result.content == big_html
    print(f"✓ fetch_url truncates oversized markdown with a recovery hint")


def _check_fetch_url_request_failure() -> None:
    import requests as _real_requests

    def _boom(*a, **kw):
        raise _real_requests.ConnectionError("boom")

    with mock.patch.object(tools.requests, "get", side_effect=_boom):
        result = tools.fetch_url("https://nope.invalid")
    assert isinstance(result, str), type(result)
    assert result.startswith("<request failed:"), result
    print(f"✓ fetch_url surfaces network failures as <request failed: ...>")


def main() -> None:
    _check_extraction_main_content()
    _check_extraction_full_document()
    _check_extraction_select_table()
    _check_extraction_select_limit()
    _check_plugin_loads_under_default_config()
    _check_fetch_url_md_default()
    _check_fetch_url_void()
    _check_fetch_url_non_html()
    _check_fetch_url_large_md_truncates()
    _check_fetch_url_request_failure()
    print("smoke_html_tools: all checks passed")


if __name__ == "__main__":
    main()
