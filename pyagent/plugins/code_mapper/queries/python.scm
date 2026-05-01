; Tags query for Python.
;
; Source: tree-sitter/tree-sitter-python @ 26855eabccb19c6abf499fbc5b8dc7cc9ab8bc64
;   queries/tags.scm — https://github.com/tree-sitter/tree-sitter-python/blob/master/queries/tags.scm
;
; Companion config: python.toml (capture → kind mapping, def node
; types, promotion rules, extension list).
;
; Capture-name convention follows tree-sitter's tags spec:
;   @name                — the identifier the symbol is bound to
;   @definition.<kind>   — the surrounding node that constitutes the def
;   @reference.<kind>    — a use site
;
; Deviations from upstream are flagged inline.

; --- upstream patterns (verbatim, except as noted) ---

; NOTE: upstream tags.scm wraps the top-level constant pattern in
; `(expression_statement ...)`, but the current grammar in
; tree-sitter-language-pack 1.6 produces `(module (assignment ...))`
; directly, so that pattern matches nothing. Adapted below to match
; module-level assignments without the obsolete wrapper.
(module
  (assignment
    left: (identifier) @name) @definition.constant)

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

(call
  function: [
      (identifier) @name
      (attribute
        attribute: (identifier) @name)
  ]) @reference.call

; --- pyagent extensions: imports ---

(import_statement
  name: (dotted_name) @name) @definition.import

(import_statement
  name: (aliased_import
    name: (dotted_name) @name)) @definition.import

(import_from_statement
  module_name: (dotted_name) @name) @definition.import

(import_from_statement
  module_name: (relative_import) @name) @definition.import
