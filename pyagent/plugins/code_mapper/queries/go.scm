; Tags query for Go.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/go-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/go-tags.scm
; License: Apache-2.0
;
; Companion config: go.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`
; plus `(#strip!)` / `(#set-adjacent!)` doc-comment predicates. We
; rewrite to bare `@name` and drop the @doc/predicate scaffolding.
; The blanket `(type_identifier) @name.reference.type @reference.type`
; capture is dropped — it fires on every type identifier in the file
; and produces an unusable amount of "type" noise for an outline.
; Per-call-site @reference.call captures are also dropped.

(function_declaration
    name: (identifier) @name) @definition.function

(method_declaration
    name: (field_identifier) @name) @definition.method

; struct → kind="struct" (mapped via go.toml's @definition.class slot,
; which we re-purpose here since Go doesn't have classes proper).
(type_declaration
    (type_spec
        name: (type_identifier) @name
        type: (struct_type))) @definition.class

(type_declaration
    (type_spec
        name: (type_identifier) @name
        type: (interface_type))) @definition.interface

; Other type aliases (function types, slices, maps, named scalar types).
(type_spec
    name: (type_identifier) @name
    type: [(function_type) (slice_type) (map_type) (pointer_type) (array_type) (channel_type) (qualified_type) (type_identifier)]) @definition.type

(package_clause
    "package"
    (package_identifier) @name) @definition.module

(var_declaration
    (var_spec
        name: (identifier) @name)) @definition.variable

(const_declaration
    (const_spec
        name: (identifier) @name)) @definition.constant
