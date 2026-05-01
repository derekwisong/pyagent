; Tags query for Scala.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-languages/scala-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-languages/scala-tags.scm
; License: Apache-2.0
;
; Companion config: scala.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`;
; we rewrite to bare `@name`. Per-call-site / per-type reference
; captures (call_expression, instance_expression, extends_clause)
; are dropped — too noisy for a symbol outline.

(package_clause
    name: (package_identifier) @name) @definition.module

(trait_definition
    name: (identifier) @name) @definition.interface

(enum_definition
    name: (identifier) @name) @definition.enum

(simple_enum_case
    name: (identifier) @name) @definition.class

(full_enum_case
    name: (identifier) @name) @definition.class

(class_definition
    name: (identifier) @name) @definition.class

(object_definition
    name: (identifier) @name) @definition.object

(function_definition
    name: (identifier) @name) @definition.function

; Abstract methods in traits are function_declaration (no body).
(function_declaration
    name: (identifier) @name) @definition.function

(val_definition
    pattern: (identifier) @name) @definition.variable

(var_definition
    pattern: (identifier) @name) @definition.variable

(val_declaration
    name: (identifier) @name) @definition.variable

(var_declaration
    name: (identifier) @name) @definition.variable

(type_definition
    name: (type_identifier) @name) @definition.type

(class_parameter
    name: (identifier) @name) @definition.property
