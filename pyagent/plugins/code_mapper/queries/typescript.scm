; Tags query for TypeScript.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-languages/typescript-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-languages/typescript-tags.scm
; License: Apache-2.0
;
; Companion config: typescript.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use the GitHub-tags
; convention `@name.definition.X` and `@reference.X`. This loader
; expects bare `@name` and a single `@definition.X` / `@reference.X`
; per match, so we rewrite captures accordingly. Reference patterns
; that emit per-identifier noise (every type annotation, every
; new_expression target) are dropped — too noisy for a symbol
; outline. Upstream also collapses interface_declaration under both
; @definition.interface and @definition.class; we keep only the
; interface tag.

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

; Top-level arrow function / function-expression bound to a const/let.
;   const handler = () => { ... }
(lexical_declaration
    (variable_declarator
        name: (identifier) @name
        value: [(arrow_function) (function_expression)])) @definition.function
