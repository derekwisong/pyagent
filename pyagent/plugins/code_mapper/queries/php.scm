; Tags query for PHP.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-languages/php-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-languages/php-tags.scm
; License: Apache-2.0
;
; Companion config: php.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`;
; we rewrite to bare `@name`. Aider also collapses
; method_declaration under @definition.function — we keep them
; distinct as @definition.method. Per-call-site reference captures
; (function_call_expression, scoped_call_expression,
; member_call_expression, object_creation_expression) are dropped.
; Added interface_declaration, trait_declaration, enum_declaration
; that aider's spec omits.

(class_declaration
    name: (name) @name) @definition.class

(interface_declaration
    name: (name) @name) @definition.interface

(trait_declaration
    name: (name) @name) @definition.trait

(enum_declaration
    name: (name) @name) @definition.enum

(function_definition
    name: (name) @name) @definition.function

(method_declaration
    name: (name) @name) @definition.method
