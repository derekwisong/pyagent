# Adding a language to code-mapper

Two files in `queries/` per language. No edit to `mapper.py`. Plugin
picks up new languages on next agent restart.

```
queries/
    <lang>.toml      # capture-name → kind mapping, def node types, etc.
    <lang>.scm       # tree-sitter query that produces the captures
```

`<lang>` MUST match a tree-sitter-language-pack id (`get_language(<lang>)`
must return a Language). See https://pypi.org/project/tree-sitter-language-pack/
for the list of available languages.

## The TOML schema

```toml
# Scalars FIRST. Anything after a [table] header belongs to that table
# until the next header — TOML rule, easy to miss.

language = "<id>"                    # tree-sitter-language-pack id
description = "..."
extensions = [".ext1", ".ext2"]      # case-insensitive, leading dot

# Node types whose presence in an ancestor walk implies "the symbol
# is inside a definition" — used for parent attribution. Empty list
# means rely entirely on @parent captures from the .scm.
definition_node_types = ["class_definition", "function_definition"]

# Plugin-internal: which docstring extractor to use. Currently only
# "python" is implemented in mapper.py:_docstring_for. Omit (or set
# to a string this code doesn't recognize) to disable.
docstrings = "python"

# Tree-sitter capture name → normalized symbol kind. Anything not
# listed here is dropped. Multiple capture names may map to the same
# kind. The `@name` and `@parent` captures are special and must NOT
# appear here — they're consumed by the loader directly.
[captures]
"definition.class" = "class"
"definition.function" = "function"
"reference.call" = "call"

# Kind promotion rules. If an emitted symbol has kind=`from` and its
# enclosing definition node type is one of `when_inside`, the kind is
# upgraded to `to`. First matching rule wins.
[[promote]]
from = "function"
to = "method"
when_inside = ["class_definition"]
```

## The .scm convention

The plugin uses `QueryCursor.matches()` (per-pattern), so every match
is processed in isolation. For each match, the loader looks for:

* `@name` — the identifier to use as the symbol's name. **Required.**
  Multiple `@name` captures in one match emit multiple symbols.
* `@definition.<kind>` *or* `@reference.<kind>` — the enclosing
  definition node. **Required.** Exactly one per pattern. The
  `<kind>` part has no meaning to the loader — it's the string you'll
  map in `[captures]`.
* `@parent` — *optional* explicit override of the enclosing-definition
  name. Use this when the AST walk would attribute to the wrong scope
  (e.g. C++ `void Foo::bar() {}` — the AST puts `bar` inside a
  namespace, but `@parent` lets you record `Foo`).

Predicates `(#match? @x "regex")` and `(#eq? @x "literal")` are
supported by tree-sitter's query engine and are useful for matching
sets of tag names (e.g. `^[hH][1-6]$` for HTML headings).

## Workflow for adding a language

1. **Find the upstream tags.scm.** Most tree-sitter grammars at
   `github.com/tree-sitter/tree-sitter-<lang>` ship `queries/tags.scm`.
   Pin the commit hash you read.

2. **Vendor it as `queries/<lang>.scm`.** Add a header comment naming
   the source repo + commit. Edit aggressively: upstream's tags.scm
   is written for the GitHub code-nav tags spec, which collapses
   distinct kinds (e.g. struct/enum/union all under
   `@definition.class`). For our purposes finer kinds are better —
   replace collapsed capture names with kind-specific ones. Mark
   deviations inline with a `; DEVIATION:` comment so future refreshes
   know what was patched.

3. **Probe the actual grammar before trusting the query.** The
   language-pack ships compiled grammars whose node types may have
   drifted from upstream's tags.scm. The plugin exposes a
   `probe_grammar` tool for exactly this — it prints the parse tree
   with field names visible (`left:`, `name:`, `body:`, etc.) so you
   write queries that reference what the grammar actually produces:

   ```
   probe_grammar(language="rust", source="impl Foo { fn bar() {} }")
   →
   source_file
     impl_item
       type: type_identifier  'Foo'
       body: declaration_list
         function_item
           name: identifier  'bar'
           parameters: parameters
           body: block
   ```

   When upstream's pattern matches nothing, the cause is almost always
   that the grammar wraps the matched node differently than the .scm
   expects. Probe a representative snippet, see the real shape, patch
   the .scm. The tool works for any language the language-pack ships,
   not just languages already configured here.

4. **Write `<lang>.toml`** mapping each capture name to a normalized
   kind. Decide on `definition_node_types` (the AST node types you
   want parent-walk to stop at) and any `[[promote]]` rules.

5. **Add a fixture + assertions** to `tests/smoke_code_mapper.py`.
   Use the `has(kind, name, parent)` helper pattern from existing
   languages — assertion shape is uniform.

6. **Run `python -m tests.smoke_code_mapper`** and iterate.

## Common gotchas

* **Capture-pattern drift.** Upstream tags.scm sometimes references
  node types or field names that don't exist in the grammar version
  the language pack ships. Symptom: the pattern simply doesn't match.
  Fix: probe the grammar (step 3 above), patch the pattern, document
  the deviation in the .scm header.

* **Two patterns capturing the same node.** `<main id="...">` matches
  both an "element with id" and a "semantic landmark" pattern. The
  per-match logic (`QueryCursor.matches()`) keeps these separate, but
  if you ever switch back to `captures()` the `@name` from one pattern
  will pair with the `@definition.X` of another. Stay on `matches()`.

* **The captured node isn't the scope-defining node.** In C/C++ the
  function name is captured inside `function_declarator`, not
  `function_definition`. Walking up from `function_declarator` hits
  `function_definition` first — but `function_definition` has no
  `name:` field and isn't a useful "scope" anyway. **Don't include
  `function_definition` in `definition_node_types`** for C/C++; let
  the walk continue to the actual surrounding class/namespace.

* **`@parent` is per-match, not per-node.** When the same AST element
  is captured by two different patterns, only the match that explicitly
  captured `@parent` carries the override. The other match still gets
  AST-walk-based parent attribution. This is intentional — see the
  HTML `<main id="…">` case where the `element_id` symbol gets
  parent="main" but the `section` symbol does not.

* **Trait impls in Rust attribute methods to the trait.** `impl Greet
  for Point { fn hello(&self) {} }` produces `method hello (in Greet)`,
  not `(in Point)`. Upstream tags.scm captures the trait name as the
  impl's @name. Fixing this would require capturing `type:` separately
  and choosing between trait and type at attribution time — out of
  scope for v1. Documented quirk, not a bug.

* **TOML scalar vs table ordering.** Once you write `[captures]` or
  `[[promote]]`, every subsequent scalar belongs to that table. Put
  `language`, `description`, `extensions`, `definition_node_types`,
  `docstrings` BEFORE any header.

## Refreshing a vendored .scm

Upstream tags.scm files barely change — manual refresh is fine. When
you do refresh:

1. Read the diff between the old commit and HEAD before pasting.
2. Re-run any `; DEVIATION:` patches against the new file.
3. Update the commit hash in the file's header.
4. Run the smoke test.
