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


_TYPESCRIPT_SRC = b'''class Foo {
  bar() { return 1; }
  static baz() { return 2; }
}
interface IThing { do(): void; }
enum Color { Red, Blue }
type Pair = [number, number];
function freeFn() {}
const arrow = () => 1;
namespace App { export const x = 1; }
'''


def _check_map_typescript() -> None:
    out = mapper.map_source(_TYPESCRIPT_SRC, "typescript", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Foo", None), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("method", "baz", "Foo"), out["symbols"]
    assert has("interface", "IThing", None), out["symbols"]
    assert has("enum", "Color", None), out["symbols"]
    assert has("type", "Pair", None), out["symbols"]
    assert has("function", "freeFn", None), out["symbols"]
    assert has("function", "arrow", None), out["symbols"]
    assert has("module", "App", None), out["symbols"]
    print(f"✓ TypeScript: class/method/interface/enum/type/function/module")


_TSX_SRC = b'''function App() { return <div>Hi</div>; }
class Comp { render() { return null; } }
const Btn = () => <button/>;
interface Props { name: string; }
'''


def _check_map_tsx() -> None:
    out = mapper.map_source(_TSX_SRC, "tsx", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("function", "App", None), out["symbols"]
    assert has("class", "Comp", None), out["symbols"]
    assert has("method", "render", "Comp"), out["symbols"]
    assert has("function", "Btn", None), out["symbols"]
    assert has("interface", "Props", None), out["symbols"]
    print(f"✓ TSX: JSX functions / class component / arrow component")


_JAVASCRIPT_SRC = b'''class Foo { bar() { return 1; } static baz() {} }
function freeFn() {}
const arrow = () => 1;
function* gen() { yield 1; }
'''


def _check_map_javascript() -> None:
    out = mapper.map_source(_JAVASCRIPT_SRC, "javascript", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Foo", None), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("method", "baz", "Foo"), out["symbols"]
    assert has("function", "freeFn", None), out["symbols"]
    assert has("function", "arrow", None), out["symbols"]
    assert has("function", "gen", None), out["symbols"]
    print(f"✓ JavaScript: class/method/function/arrow/generator")


_GO_SRC = b'''package main

import "fmt"

type Point struct { X, Y int }
type Greeter interface { Hello() string }
type Handler func(int) error

const Pi = 3.14
var Verbose = false

func (p *Point) Distance() int { return 0 }
func main() { fmt.Println("hi") }
'''


def _check_map_go() -> None:
    out = mapper.map_source(_GO_SRC, "go", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("module", "main", None), out["symbols"]
    assert has("struct", "Point", None), out["symbols"]
    assert has("interface", "Greeter", None), out["symbols"]
    assert has("type", "Handler", None), out["symbols"]
    assert has("constant", "Pi", None), out["symbols"]
    assert has("variable", "Verbose", None), out["symbols"]
    # Go's grammar puts the receiver outside method_declaration, so
    # parent attribution to the receiver type isn't trivially
    # available; document the as-is behavior.
    distance = [s for s in out["symbols"] if s["name"] == "Distance"]
    assert len(distance) == 1 and distance[0]["kind"] == "method", distance
    assert has("function", "main", None), out["symbols"]
    print(f"✓ Go: package/struct/interface/type/const/var/method/function")


_JAVA_SRC = b'''package com.example;

public class Foo {
    private int count;
    public Foo() { this.count = 0; }
    public void bar() {}
    public static int baz() { return 1; }
}

interface IThing { void doIt(); }
enum Color { RED, GREEN }
record Pair(int x, int y) {}
'''


def _check_map_java() -> None:
    out = mapper.map_source(_JAVA_SRC, "java", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Foo", None), out["symbols"]
    assert has("field", "count", "Foo"), out["symbols"]
    assert has("constructor", "Foo", "Foo"), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("method", "baz", "Foo"), out["symbols"]
    assert has("interface", "IThing", None), out["symbols"]
    assert has("method", "doIt", "IThing"), out["symbols"]
    assert has("enum", "Color", None), out["symbols"]
    assert has("record", "Pair", None), out["symbols"]
    print(f"✓ Java: class/field/method/constructor/interface/enum/record")


_BASH_SRC = b'''#!/bin/bash
PORT=8080
NAME="world"

function greet() {
    echo "hello $NAME"
}

run_server() {
    python -m http.server $PORT
}
'''


def _check_map_bash() -> None:
    out = mapper.map_source(_BASH_SRC, "bash", kind="all")
    assert out["errors"] == [], out["errors"]
    by_name = {s["name"]: s for s in out["symbols"]}
    assert by_name["PORT"]["kind"] == "variable", by_name["PORT"]
    assert by_name["NAME"]["kind"] == "variable", by_name["NAME"]
    assert by_name["greet"]["kind"] == "function", by_name["greet"]
    assert by_name["run_server"]["kind"] == "function", by_name["run_server"]
    print(f"✓ Bash: function + variable_assignment")


_RUBY_SRC = b'''module Greeter
  class Animal
    def speak
      "hi"
    end

    def self.species
      "mammal"
    end
  end
end

def top_level
  1
end
'''


def _check_map_ruby() -> None:
    out = mapper.map_source(_RUBY_SRC, "ruby", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("module", "Greeter", None), out["symbols"]
    assert has("class", "Animal", "Greeter"), out["symbols"]
    assert has("method", "speak", "Animal"), out["symbols"]
    assert has("method", "species", "Animal"), out["symbols"]
    assert has("method", "top_level", None), out["symbols"]
    print(f"✓ Ruby: module/class/method (incl. singleton methods)")


_JSON_SRC = b'''{
  "name": "foo",
  "version": "1.0",
  "deps": {
    "left-pad": "1.0",
    "react": "18.0"
  }
}'''


def _check_map_json() -> None:
    out = mapper.map_source(_JSON_SRC, "json", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("field", "name", None), out["symbols"]
    assert has("field", "deps", None), out["symbols"]
    assert has("field", "left-pad", "deps"), out["symbols"]
    assert has("field", "react", "deps"), out["symbols"]
    print(f"✓ JSON: top-level + nested fields with parent attribution")


_YAML_SRC = b'''name: my-app
version: 1.0
deps:
  left-pad: 1.0
  react:
    version: 18.0
    peer: true
'''


def _check_map_yaml() -> None:
    out = mapper.map_source(_YAML_SRC, "yaml", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("field", "name", None), out["symbols"]
    assert has("field", "deps", None), out["symbols"]
    assert has("field", "left-pad", "deps"), out["symbols"]
    assert has("field", "react", "deps"), out["symbols"]
    assert has("field", "version", "react"), out["symbols"]
    print(f"✓ YAML: nested mapping keys with parent attribution")


_TOML_SRC = b'''name = "my-app"
version = "1.0"

[deps]
left-pad = "1.0"
react = "18.0"

[[plugins]]
id = "a"

[deps.dev]
pytest = "7.0"
'''


def _check_map_toml() -> None:
    out = mapper.map_source(_TOML_SRC, "toml", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("field", "name", None), out["symbols"]
    assert has("module", "deps", None), out["symbols"]
    assert has("field", "left-pad", "deps"), out["symbols"]
    assert has("field", "react", "deps"), out["symbols"]
    assert has("module", "plugins", None), out["symbols"]
    assert has("field", "id", "plugins"), out["symbols"]
    assert has("module", "deps.dev", None), out["symbols"]
    assert has("field", "pytest", "deps.dev"), out["symbols"]
    print(f"✓ TOML: section headers (incl. dotted) + pairs with attribution")


_MARKDOWN_SRC = b'''# Title

Some intro text.

## Section A

### Subsection A.1

text

## Section B

#### too-deep heading
'''


def _check_map_markdown() -> None:
    out = mapper.map_source(_MARKDOWN_SRC, "markdown", kind="all")
    # Markdown grammar is loose; tolerate non-fatal "errors" from
    # block-level parser quirks but require the headings.
    names = {s["name"] for s in out["symbols"]}
    assert "Title" in names, out["symbols"]
    assert "Section A" in names, out["symbols"]
    assert "Subsection A.1" in names, out["symbols"]
    assert "Section B" in names, out["symbols"]
    # H4 deliberately omitted from outline.
    assert "too-deep heading" not in names, out["symbols"]
    kinds = {s["kind"] for s in out["symbols"]}
    assert kinds == {"heading"}, kinds
    print(f"✓ Markdown: H1-H3 headings, H4+ excluded")


_DOCKERFILE_SRC = b'''FROM python:3.11 AS builder
WORKDIR /app
COPY . .
RUN pip install -r reqs.txt

FROM builder AS final
USER nobody
ENV PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
'''


def _check_map_dockerfile() -> None:
    out = mapper.map_source(_DOCKERFILE_SRC, "dockerfile", kind="all")
    assert out["errors"] == [], out["errors"]
    by = {(s["kind"], s["name"]) for s in out["symbols"]}
    assert ("stage", "builder") in by, by
    assert ("stage", "final") in by, by
    assert ("directive", "WORKDIR") in by, by
    assert ("directive", "RUN") in by, by
    assert ("directive", "ENV") in by, by
    assert ("directive", "CMD") in by, by
    print(f"✓ Dockerfile: stages + per-instruction directives")


_SWIFT_SRC = b'''class Animal {
    var name: String = ""
    func speak() {}
}

protocol Greet {
    func hi()
}

func top() {}
'''


def _check_map_swift() -> None:
    out = mapper.map_source(_SWIFT_SRC, "swift", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Animal", None), out["symbols"]
    assert has("property", "name", "Animal"), out["symbols"]
    assert has("method", "speak", "Animal"), out["symbols"]
    assert has("interface", "Greet", None), out["symbols"]
    assert has("method", "hi", "Greet"), out["symbols"]
    assert has("function", "top", None), out["symbols"]
    print(f"✓ Swift: class/property/method/protocol/function")


_KOTLIN_SRC = b'''class Foo {
    fun bar() = 1
    fun baz(x: Int): String = "hi"
}

object Greeter {
    fun greet() = "hi"
}

fun top() = 2
'''


def _check_map_kotlin() -> None:
    out = mapper.map_source(_KOTLIN_SRC, "kotlin", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Foo", None), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("method", "baz", "Foo"), out["symbols"]
    assert has("object", "Greeter", None), out["symbols"]
    assert has("method", "greet", "Greeter"), out["symbols"]
    assert has("function", "top", None), out["symbols"]
    print(f"✓ Kotlin: class/method/object/function")


_SCALA_SRC = b'''package mypkg

class Foo(val x: Int) {
  def bar(): Int = x
}

object Bar {
  def hi() = 1
}

trait Greeter {
  def greet(): String
}

def topLevel() = 1
'''


def _check_map_scala() -> None:
    out = mapper.map_source(_SCALA_SRC, "scala", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("module", "mypkg", None), out["symbols"]
    assert has("class", "Foo", None), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("object", "Bar", None), out["symbols"]
    assert has("method", "hi", "Bar"), out["symbols"]
    assert has("trait", "Greeter", None), out["symbols"]
    assert has("method", "greet", "Greeter"), out["symbols"]
    assert has("function", "topLevel", None), out["symbols"]
    print(f"✓ Scala: package/class/object/trait/method/function")


_LUA_SRC = b'''function top()
  return 1
end

function t.bar()
  return 2
end

function obj:meth()
  return 3
end

local f = function() return 4 end

local M = {
  helper = function() end,
}
'''


def _check_map_lua() -> None:
    out = mapper.map_source(_LUA_SRC, "lua", kind="all")
    assert out["errors"] == [], out["errors"]
    by_name = {s["name"]: s for s in out["symbols"]}
    assert by_name["top"]["kind"] == "function", by_name["top"]
    assert by_name["bar"]["kind"] == "function", by_name["bar"]
    assert by_name["meth"]["kind"] == "method", by_name["meth"]
    assert by_name["f"]["kind"] == "function", by_name["f"]
    assert by_name["helper"]["kind"] == "function", by_name["helper"]
    print(f"✓ Lua: function (plain/dotted/local/table) + method (colon)")


_PHP_SRC = b'''<?php

class Foo {
    public function bar() { return 1; }
    private function baz() { return 2; }
}

interface IThing { public function doIt(); }
trait Greetable { public function greet() {} }
function top() { return 3; }
'''


def _check_map_php() -> None:
    out = mapper.map_source(_PHP_SRC, "php", kind="all")
    assert out["errors"] == [], out["errors"]
    has = lambda kind, name, parent: any(
        s["kind"] == kind and s["name"] == name and s["parent"] == parent
        for s in out["symbols"]
    )
    assert has("class", "Foo", None), out["symbols"]
    assert has("method", "bar", "Foo"), out["symbols"]
    assert has("method", "baz", "Foo"), out["symbols"]
    assert has("interface", "IThing", None), out["symbols"]
    assert has("method", "doIt", "IThing"), out["symbols"]
    assert has("trait", "Greetable", None), out["symbols"]
    assert has("method", "greet", "Greetable"), out["symbols"]
    assert has("function", "top", None), out["symbols"]
    print(f"✓ PHP: class/method/interface/trait/function")


_CSS_SRC = b'''.btn { color: red; }
#header { background: blue; }
.btn-primary { color: white; }

@media (min-width: 600px) {
  .x { color: blue; }
}

@keyframes spin {
  from { transform: rotate(0); }
  to { transform: rotate(360deg); }
}

@font-face {
  src: url(x.ttf);
}
'''


def _check_map_css() -> None:
    out = mapper.map_source(_CSS_SRC, "css", kind="all")
    assert out["errors"] == [], out["errors"]
    by = {(s["kind"], s["name"]) for s in out["symbols"]}
    assert ("class_selector", "btn") in by, by
    assert ("id_selector", "header") in by, by
    assert ("class_selector", "btn-primary") in by, by
    assert ("at_rule", "@media") in by, by
    assert ("at_rule", "spin") in by, by
    assert ("at_rule", "@font-face") in by, by
    print(f"✓ CSS: class/id selectors + at-rules (@media / @keyframes / @font-face)")


_SQL_SRC = b'''CREATE TABLE users (id INT, name TEXT);
CREATE VIEW recent_users AS SELECT * FROM users;
CREATE INDEX idx_users_name ON users(name);
CREATE FUNCTION fn() RETURNS INT AS $$ SELECT 1 $$ LANGUAGE sql;
CREATE SCHEMA reporting;
CREATE TRIGGER audit_trg BEFORE INSERT ON users FOR EACH ROW EXECUTE FUNCTION fn();
'''


def _check_map_sql() -> None:
    out = mapper.map_source(_SQL_SRC, "sql", kind="all")
    # tree-sitter-sql tolerates a few quirks; require schema-defining
    # symbols but allow non-fatal errors.
    by = {(s["kind"], s["name"]) for s in out["symbols"]}
    assert ("table", "users") in by, by
    assert ("view", "recent_users") in by, by
    assert ("index", "idx_users_name") in by, by
    assert ("function", "fn") in by, by
    assert ("schema", "reporting") in by, by
    assert ("trigger", "audit_trg") in by, by
    print(f"✓ SQL: table/view/index/function/schema/trigger")


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
    """The plugin's extension table covers all configured languages."""
    exts = set(mapper.supported_extensions())
    for required in (
        # v1 base set
        ".py", ".pyi",
        ".rs",
        ".c", ".h",
        ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx",
        ".html", ".htm",
        # tier 1 — most common
        ".ts", ".mts", ".cts",
        ".tsx",
        ".js", ".mjs", ".cjs", ".jsx",
        ".go",
        ".java",
        ".sh", ".bash",
        ".rb",
        # tier 2 — config / data formats
        ".json",
        ".yaml", ".yml",
        ".toml",
        ".md", ".markdown",
        ".dockerfile",
        # tier 3 — broader coverage
        ".swift",
        ".kt", ".kts",
        ".scala", ".sc",
        ".lua",
        ".php",
        ".css",
        ".sql",
    ):
        assert required in exts, (required, exts)
    print(f"✓ supported_extensions() covers all language packs: {sorted(exts)}")


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
    # tier 1 language pack
    _check_map_typescript()
    _check_map_tsx()
    _check_map_javascript()
    _check_map_go()
    _check_map_java()
    _check_map_bash()
    _check_map_ruby()
    # tier 2 — data formats
    _check_map_json()
    _check_map_yaml()
    _check_map_toml()
    _check_map_markdown()
    _check_map_dockerfile()
    # tier 3 — broader coverage
    _check_map_swift()
    _check_map_kotlin()
    _check_map_scala()
    _check_map_lua()
    _check_map_php()
    _check_map_css()
    _check_map_sql()
    _check_probe_grammar()
    _check_extension_dispatch()
    print("smoke_code_mapper: all checks passed")


if __name__ == "__main__":
    main()
