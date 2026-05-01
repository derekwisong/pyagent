; Tags query for Kotlin.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-languages/kotlin-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-languages/kotlin-tags.scm
; License: Apache-2.0
;
; Companion config: kotlin.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`;
; we rewrite to bare `@name`. The `delegation_specifier` and
; per-call-site `call_expression` reference patterns are dropped —
; too noisy for an outline.

(class_declaration
    (type_identifier) @name) @definition.class

(object_declaration
    (type_identifier) @name) @definition.object

(function_declaration
    (simple_identifier) @name) @definition.function
