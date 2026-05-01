; Tags query for TSX (TypeScript + JSX).
;
; CUSTOM (not vendored): no upstream tags.scm exists for tsx —
; tree-sitter-typescript ships only highlights/locals/injections, and
; aider/nvim-treesitter ship typescript-tags but not tsx-tags. The
; tsx grammar uses the same node types as typescript, so this file
; mirrors typescript.scm verbatim. Companion config: tsx.toml.
; License (style/conventions): Apache-2.0 (matches typescript.scm
; lineage).

(function_declaration
    name: (identifier) @name) @definition.function

(method_definition
    name: (property_identifier) @name) @definition.method

(abstract_method_signature
    name: (property_identifier) @name) @definition.method

(method_signature
    name: (property_identifier) @name) @definition.method

(function_signature
    name: (identifier) @name) @definition.function

(class_declaration
    name: (type_identifier) @name) @definition.class

(abstract_class_declaration
    name: (type_identifier) @name) @definition.class

(interface_declaration
    name: (type_identifier) @name) @definition.interface

(enum_declaration
    name: (identifier) @name) @definition.enum

(type_alias_declaration
    name: (type_identifier) @name) @definition.type

(internal_module
    name: (identifier) @name) @definition.module

; Top-level arrow / function-expression bound to const/let — the
; canonical React component shape.
(lexical_declaration
    (variable_declarator
        name: (identifier) @name
        value: [(arrow_function) (function_expression)])) @definition.function
