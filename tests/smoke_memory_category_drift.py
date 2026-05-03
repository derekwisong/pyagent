"""Smoke for the Tier-2 memory category-drift improvements.

Locks four behaviors:

  1. **`_extract_categories` walks `## <heading>` lines correctly.**
     Returns names in document order, deduplicates case-insensitively,
     ignores non-H2 lines.
  2. **`_find_similar_category` flags near-but-not-equal matches.**
     "Style" vs "Code Style" / "Database" vs "Databases" trigger;
     "Style" vs "Stack" doesn't; exact case-insensitive matches
     return None (those collapse via existing matching).
  3. **`add_memory` refuses close-existing categories**, with an
     override path via `force_new_category=True`.
  4. **`render_memory_index` prepends a compact "Categories in use"
     summary** when ≥ 5 categories exist; below threshold, the
     rendered text is unchanged.

Run with:
    .venv/bin/python -m tests.smoke_memory_category_drift
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pyagent.plugins.memory_markdown import (
    _CATEGORY_SUMMARY_MIN,
    _extract_categories,
    _find_similar_category,
)
from pyagent.plugins.memory_markdown import register as md_register


def _check_extract_categories() -> None:
    text = (
        "# Memory\n\n"
        "Some preamble.\n\n"
        "## Stack\n\n"
        "- [a](a.md) — x\n\n"
        "## Database\n"
        "- [b](b.md) — y\n\n"
        "## stack\n"  # case-insensitive duplicate; should be ignored
        "- [c](c.md) — z\n\n"
        "## Style\n"
        "- [d](d.md) — w\n"
    )
    cats = _extract_categories(text)
    assert cats == ["Stack", "Database", "Style"], cats
    print("✓ _extract_categories: ordered, dedup case-insensitive")


def _check_find_similar_category() -> None:
    """Flag near-but-not-equal matches; ignore exact and far-apart."""
    existing = ["Stack", "Database", "Style", "Gotchas"]

    # Exact case-insensitive match → None (handled by existing
    # case-insensitive collapse).
    assert _find_similar_category("Stack", existing) is None
    assert _find_similar_category("STACK", existing) is None
    assert _find_similar_category("stack", existing) is None

    # Near misses → flagged.
    assert _find_similar_category("Stacks", existing) == "Stack"
    assert _find_similar_category("Databases", existing) == "Database"
    assert _find_similar_category("Styles", existing) == "Style"
    assert _find_similar_category("Code Style", existing) == "Style"
    assert _find_similar_category("Gotcha", existing) == "Gotchas"

    # Genuinely different short names → None.
    assert _find_similar_category("Architecture", existing) is None
    assert _find_similar_category("References", existing) is None
    assert _find_similar_category("Decisions", existing) is None

    # Empty inputs are safe.
    assert _find_similar_category("", existing) is None
    assert _find_similar_category("Stack", []) is None
    print("✓ _find_similar_category: near hits flagged; exact/far ignored")


def _check_add_memory_refuses_close_category() -> None:
    """`add_memory` refuses a close-but-not-equal new category and
    points at the existing match. `force_new_category=True` overrides.
    """
    captured: dict = {"tools": {}}

    class _FakeAPI:
        _tmp = Path(tempfile.mkdtemp(prefix="pyagent-mem-drift-"))

        @property
        def user_data_dir(self):
            return self._tmp

        @property
        def config_dir(self):
            return self._tmp

        def log(self, level, msg):
            pass

        def register_tool(self, name, fn):
            captured["tools"][name] = fn

        def register_prompt_section(self, name, renderer, *, volatile=False):
            pass

        def on_session_start(self, fn):
            pass

    api = _FakeAPI()
    md_register(api)
    add_memory = captured["tools"]["add_memory"]

    # Seed an existing memory under "Style".
    out = add_memory(
        "Style", "py conventions", "python_style.md", "py style", "body"
    )
    assert "added index entry" in out, out

    # Drift attempt: "Code Style" is close to "Style" → refused.
    out = add_memory(
        "Code Style", "more py conventions", "python_style_2.md", "h", "body"
    )
    assert out.startswith("<category 'Code Style' is close to existing"), out
    assert "'Style'" in out, out
    assert "force_new_category=True" in out, out

    # Override: pass force_new_category=True → succeeds.
    out = add_memory(
        "Code Style",
        "more py conventions",
        "python_style_2.md",
        "h",
        "body",
        force_new_category=True,
    )
    assert "added index entry" in out, out

    # Genuine new category (not close to any existing) → succeeds
    # without override.
    out = add_memory(
        "Architecture",
        "service boundaries",
        "service_boundaries.md",
        "h",
        "body",
    )
    assert "added index entry" in out, out

    # Exact case-insensitive match → falls through to existing
    # case-insensitive collapse, no drift warning.
    out = add_memory(
        "STYLE",  # matches existing "Style"
        "another py conventions",
        "python_style_3.md",
        "h",
        "body",
    )
    assert "added index entry" in out, out
    assert "close to existing" not in out, out

    print("✓ add_memory: refuses close-existing; force_new_category=True overrides")


def _check_render_summary_above_threshold() -> None:
    """`render_memory_index` prepends 'Categories in use: ...' once
    the index has at least `_CATEGORY_SUMMARY_MIN` headings."""
    from pyagent.plugins import memory_markdown as md_mod

    # Reach into render_memory_index by invoking register() with a
    # fake api against a tempdir, then grabbing the captured renderer.
    sections: dict = {}

    class _FakeAPI:
        _tmp = Path(tempfile.mkdtemp(prefix="pyagent-mem-render-"))

        @property
        def user_data_dir(self):
            return self._tmp

        @property
        def config_dir(self):
            return self._tmp

        def log(self, level, msg):
            pass

        def register_tool(self, name, fn):
            pass

        def register_prompt_section(self, name, renderer, *, volatile=False):
            sections[name] = renderer

        def on_session_start(self, fn):
            pass

    api = _FakeAPI()
    md_register(api)
    render_index = sections["memory-index"]

    # Below threshold: 3 categories → no summary.
    storage = api._tmp
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "MEMORY.md").write_text(
        "# Memory\n\n"
        "## Stack\n- [a](a.md) — x\n\n"
        "## Database\n- [b](b.md) — y\n\n"
        "## Style\n- [c](c.md) — z\n"
    )
    rendered = render_index(None)
    assert "Categories in use" not in rendered, rendered

    # Above threshold: build an index with the configured minimum +
    # one to be unambiguous.
    cats = ["Architecture", "Database", "Decisions", "Gotchas", "Style", "Stack"]
    assert len(cats) >= _CATEGORY_SUMMARY_MIN
    index_text = "# Memory\n\n"
    for c in cats:
        index_text += f"## {c}\n- [x](x_{c.lower()}.md) — y\n\n"
    (storage / "MEMORY.md").write_text(index_text)
    rendered = render_index(None)
    assert "Categories in use" in rendered, rendered
    # Summary lists categories alphabetically; spot-check.
    for c in cats:
        assert c in rendered, (c, rendered)
    # Summary sits right after the H1 line.
    head_index = rendered.find("# Memory")
    summary_index = rendered.find("Categories in use")
    assert head_index < summary_index, (head_index, summary_index)
    # Source file unchanged — derived line is render-only.
    on_disk = (storage / "MEMORY.md").read_text()
    assert "Categories in use" not in on_disk, on_disk
    print(
        f"✓ render_memory_index: 'Categories in use' prepended at "
        f">= {_CATEGORY_SUMMARY_MIN} cats; source file untouched"
    )


def main() -> None:
    _check_extract_categories()
    _check_find_similar_category()
    _check_add_memory_refuses_close_category()
    _check_render_summary_above_threshold()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
