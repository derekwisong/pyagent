"""End-to-end smoke for the code-mapper plugin.

Three concerns:

  1. **Plugin loads under default config.** With "code-mapper" in
     built_in_plugins_enabled, `discover()` and `load()` produce the
     `map_code` tool.

  2. **Maps a clean Python file correctly.** Symbol kinds, names, parents,
     1-indexed line numbers, docstring opt-in.

  3. **Degrades gracefully.** Broken-syntax file still produces partial
     symbols + non-empty `errors`. Unsupported extension returns the
     `error` payload, not an exception.

Run with:

    .venv/bin/python -m tests.smoke_code_mapper
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from pyagent import (
    config as config_mod,
    paths as paths_mod,
    plugins,
)
from pyagent.plugins.code_mapper import mapper


_CLEAN_PY = b'''"""Module docstring."""

import os
from pathlib import Path

CONST = 42

class Animal:
    """An animal."""

    def speak(self):
        """Make a sound."""
        return "..."

    def name(self):
        return "anon"


def top_level():
    """A top-level function."""
    return 1


def _nested_outer():
    def inner():
        return 0
    return inner
'''


_BROKEN_PY = b'''import os

def good():
    return 1

class Half:
    def fine(self):
        return 2

    def broken(self
        # missing close-paren and body
'''


def _check_map_clean_python() -> None:
    """Symbols, kinds, parents, lines all line up for a normal file."""
    out = mapper.map_source(_CLEAN_PY, "python", kind="all")
    assert out["language"] == "python", out
    assert out["errors"] == [], out["errors"]
    syms = out["symbols"]
    by_name = {s["name"]: s for s in syms}

    # Imports.
    assert by_name["os"]["kind"] == "import", by_name["os"]
    assert by_name["os"]["line"] == 3, by_name["os"]
    assert by_name["pathlib"]["kind"] == "import", by_name["pathlib"]
    assert by_name["pathlib"]["line"] == 4, by_name["pathlib"]

    # Top-level constant.
    assert by_name["CONST"]["kind"] == "constant", by_name["CONST"]
    assert by_name["CONST"]["line"] == 6, by_name["CONST"]

    # Class + methods.
    assert by_name["Animal"]["kind"] == "class", by_name["Animal"]
    assert by_name["Animal"]["parent"] is None
    assert by_name["Animal"]["line"] == 8, by_name["Animal"]

    speak = by_name["speak"]
    assert speak["kind"] == "method", speak
    assert speak["parent"] == "Animal", speak
    assert speak["line"] == 11, speak

    name_method = by_name["name"]
    assert name_method["kind"] == "method", name_method
    assert name_method["parent"] == "Animal", name_method

    # Top-level function.
    tl = by_name["top_level"]
    assert tl["kind"] == "function", tl
    assert tl["parent"] is None, tl
    assert tl["line"] == 19, tl

    # Nested function: parent is the enclosing function, not None.
    inner = by_name["inner"]
    assert inner["kind"] == "function", inner
    assert inner["parent"] == "_nested_outer", inner

    print(
        f"✓ clean Python: {len(syms)} symbols mapped "
        f"(class/method/function/import/constant)"
    )


def _check_kind_filter() -> None:
    out = mapper.map_source(_CLEAN_PY, "python", kind="imports")
    kinds = {s["kind"] for s in out["symbols"]}
    assert kinds == {"import"}, kinds

    out = mapper.map_source(_CLEAN_PY, "python", kind="functions")
    kinds = {s["kind"] for s in out["symbols"]}
    # functions filter pulls in both functions and methods.
    assert kinds <= {"function", "method"}, kinds
    assert "function" in kinds and "method" in kinds, kinds

    out = mapper.map_source(_CLEAN_PY, "python", kind="classes")
    kinds = {s["kind"] for s in out["symbols"]}
    assert kinds == {"class"}, kinds

    print(f"✓ kind= filter narrows symbol set as documented")


def _check_docstrings_optin() -> None:
    out_off = mapper.map_source(_CLEAN_PY, "python", kind="functions")
    assert all("docstring" not in s for s in out_off["symbols"]), (
        out_off["symbols"]
    )

    out_on = mapper.map_source(
        _CLEAN_PY, "python", kind="functions", include_docstrings=True
    )
    by_name = {s["name"]: s for s in out_on["symbols"]}
    assert by_name["speak"]["docstring"].strip() == "Make a sound.", (
        by_name["speak"]
    )
    assert by_name["top_level"]["docstring"].strip() == (
        "A top-level function."
    ), by_name["top_level"]
    # `name` method has no docstring → field absent or None.
    name_doc = by_name["name"].get("docstring")
    assert name_doc is None, name_doc
    print(f"✓ include_docstrings=True attaches docstrings; default omits them")


def _check_broken_python_degrades() -> None:
    """Tree-sitter recovers around the broken region — the `good`
    function and the `Half.fine` method should still appear, and
    `errors` should be non-empty."""
    out = mapper.map_source(_BROKEN_PY, "python", kind="all")
    names = {s["name"] for s in out["symbols"]}
    assert "good" in names, names
    assert "fine" in names, names
    assert len(out["errors"]) > 0, out["errors"]
    # Errors should carry a positive line number.
    assert all(e["line"] >= 0 for e in out["errors"]), out["errors"]
    print(
        f"✓ broken Python: {len(out['symbols'])} symbols recovered, "
        f"{len(out['errors'])} error(s) reported"
    )


def _check_unsupported_extension() -> None:
    """`map_code_for_path` should return a clean error payload, not
    raise, for an extension we don't ship a grammar/query for."""
    out_str = mapper.map_code_for_path(
        "/tmp/whatever.foo", b"junk", kind="all"
    )
    payload = json.loads(out_str)
    assert "error" in payload, payload
    assert ".foo" in payload["error"], payload["error"]
    print(f"✓ unsupported extension → clean error payload")


def _check_plugin_loads_under_default_config() -> None:
    """With the default config, code-mapper is in
    built_in_plugins_enabled and load() exposes the map_code tool."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-codemapper-"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
            assert "code-mapper" in cfg["built_in_plugins_enabled"], (
                cfg["built_in_plugins_enabled"]
            )
            loaded = plugins.load()
            tool_names = set(loaded.tools().keys())
    assert "map_code" in tool_names, tool_names
    print(f"✓ plugin loads by default; map_code in tools = {sorted(tool_names)}")


def _check_end_to_end_against_real_file() -> None:
    """As-if-the-agent: feed mapper a real file from this repo and
    confirm the output is well-formed JSON with believable symbols."""
    target = Path(__file__).resolve().parents[1] / "pyagent" / "agent.py"
    if not target.exists():
        print(f"  (skipped: {target} not found)")
        return
    out_str = mapper.map_code_for_path(
        str(target), target.read_bytes(), kind="classes"
    )
    payload = json.loads(out_str)
    assert payload["file"] == str(target)
    assert payload["language"] == "python", payload
    # pyagent/agent.py defines Agent — should appear under kind=classes.
    names = [s["name"] for s in payload["symbols"]]
    assert "Agent" in names, names
    print(f"✓ end-to-end on pyagent/agent.py → classes: {names}")


_RUST_SRC = b'''use std::io;

struct Point { x: i32, y: i32 }

enum Color { Red, Green }

trait Greet { fn hello(&self); }

mod nested {
    pub fn inside() {}
}

impl Point {
    fn new() -> Self { Point { x: 0, y: 0 } }
}

impl Greet for Point {
    fn hello(&self) {}
}

fn top() {}
'''


def _check_map_rust() -> None:
    out = mapper.map_source(_RUST_SRC, "rust", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )

    # ADTs split into kind-specific buckets (NOT collapsed under "class").
    assert has("struct", "Point", None), out["symbols"]
    assert has("enum", "Color", None), out["symbols"]
    assert has("trait", "Greet", None), out["symbols"]

    # Function inside `mod` stays a function (NOT promoted to method).
    assert has("function", "inside", "nested"), out["symbols"]

    # Function inside `impl` becomes a method, attributed to Point.
    assert has("method", "new", "Point"), out["symbols"]

    # Trait impl: tags.scm captures the trait name as the impl's @name,
    # so methods inside attribute to the trait — known convention,
    # documented in EXTENDING.md.
    assert has("method", "hello", "Greet"), out["symbols"]

    # Both impl blocks captured.
    assert has("impl", "Point", None), out["symbols"]
    assert has("impl", "Greet", None), out["symbols"]

    assert has("function", "top", None), out["symbols"]
    print(f"✓ Rust: struct/enum/trait/method/function classified correctly")


_C_SRC = b'''#include <stdio.h>

typedef struct point { int x, y; } point_t;

enum color { RED, GREEN, BLUE };

union token { int i; float f; };

int add(int a, int b) { return a + b; }

static void greet(const char *name) { printf("hi"); }
'''


def _check_map_c() -> None:
    out = mapper.map_source(_C_SRC, "c", kind="all")
    assert out["errors"] == [], out["errors"]
    by_name = {s["name"]: s for s in out["symbols"]}
    assert by_name["point"]["kind"] == "struct", by_name["point"]
    assert by_name["point_t"]["kind"] == "typedef", by_name["point_t"]
    assert by_name["color"]["kind"] == "enum", by_name["color"]
    assert by_name["token"]["kind"] == "union", by_name["token"]
    assert by_name["add"]["kind"] == "function", by_name["add"]
    assert by_name["greet"]["kind"] == "function", by_name["greet"]
    print(f"✓ C: struct/typedef/enum/union/function classified correctly")


_CPP_SRC = b'''namespace app {

class Animal {
public:
    Animal();
    void speak() const;
};

void Animal::speak() const {}

struct Point { int x, y; };

void top() {}

}  // namespace app
'''


def _check_map_cpp() -> None:
    out = mapper.map_source(_CPP_SRC, "cpp", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("namespace", "app", None), out["symbols"]
    assert has("class", "Animal", "app"), out["symbols"]
    assert has("struct", "Point", "app"), out["symbols"]
    # Both the in-class declaration and the out-of-line definition of
    # `Animal::speak` attribute to Animal — the latter via @parent.
    speak_in_animal = [
        s
        for s in out["symbols"]
        if s["kind"] == "method"
        and s["name"] == "speak"
        and s["parent"] == "Animal"
    ]
    assert len(speak_in_animal) == 2, speak_in_animal
    # Free function inside namespace.
    assert has("function", "top", "app"), out["symbols"]
    print(f"✓ C++: class/struct/method (in-class + out-of-line)/namespace OK")


_HTML_SRC = b'''<!doctype html>
<html><body>
<main id="top">
  <h1>Title</h1>
  <h2>Sub</h2>
  <section id="intro"><p>hi</p></section>
  <nav><a href="#">link</a></nav>
  <script>console.log(1);</script>
  <style>p { color: red; }</style>
</main>
</body></html>
'''


def _check_map_html() -> None:
    out = mapper.map_source(_HTML_SRC, "html", kind="all")
    assert out["errors"] == [], out["errors"]
    by = {(s["kind"], s["name"]): s for s in out["symbols"]}
    # Headings.
    assert ("heading", "Title") in by, list(by)
    assert ("heading", "Sub") in by, list(by)
    # Element ids carry the owning tag as their parent.
    top = by[("element_id", "top")]
    assert top["parent"] == "main", top
    intro = by[("element_id", "intro")]
    assert intro["parent"] == "section", intro
    # Semantic landmarks.
    assert ("section", "main") in by, list(by)
    assert ("section", "section") in by, list(by)
    assert ("section", "nav") in by, list(by)
    # script/style blocks.
    assert ("script", "script") in by, list(by)
    assert ("style", "style") in by, list(by)
    print(
        f"✓ HTML: {len(out['symbols'])} outline symbols "
        f"(headings/ids/landmarks/script/style)"
    )


def _check_probe_grammar() -> None:
    """probe_grammar prints the parse tree with field names visible,
    works for languages without a config (the whole point), and
    bounds output via max_nodes."""
    out = mapper.probe_grammar("python", "CONST = 1")
    assert "module" in out, out
    assert "assignment" in out, out
    # Field names must show — they're what queries reference.
    assert "left:" in out, out
    assert "right:" in out, out
    # Token leaves carry their text.
    assert "'CONST'" in out, out

    # Languages without a code-mapper config still probe — that's
    # what the agent does when adding a new language.
    out = mapper.probe_grammar("go", "func main() {}")
    assert "function_declaration" in out or "function_definition" in out, out

    # max_nodes bounds output.
    out = mapper.probe_grammar(
        "python", "a=1\nb=2\nc=3\nd=4\ne=5", max_nodes=4
    )
    assert "truncated" in out, out

    # Unknown language returns a clean error marker, not an exception.
    err = mapper.probe_grammar("klingon", "qapla")
    assert err.startswith("<unknown language"), err
    print(f"✓ probe_grammar: field names visible, bounded, language-pack-direct")


def _check_extension_dispatch() -> None:
    """The plugin's extension table covers all four languages."""
    exts = set(mapper.supported_extensions())
    for required in (
        ".py", ".pyi",
        ".rs",
        ".c", ".h",
        ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
        ".html", ".htm",
    ):
        assert required in exts, (required, exts)
    print(f"✓ supported_extensions() covers Python/Rust/C/C++/HTML: {sorted(exts)}")


def main() -> None:
    _check_map_clean_python()
    _check_kind_filter()
    _check_docstrings_optin()
    _check_broken_python_degrades()
    _check_unsupported_extension()
    _check_plugin_loads_under_default_config()
    _check_end_to_end_against_real_file()
    _check_map_rust()
    _check_map_c()
    _check_map_cpp()
    _check_map_html()
    _check_probe_grammar()
    _check_extension_dispatch()
    print("smoke_code_mapper: all checks passed")


if __name__ == "__main__":
    main()
