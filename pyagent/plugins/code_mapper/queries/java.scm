; Tags query for Java.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/java-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/java-tags.scm
; License: Apache-2.0
;
; Companion config: java.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`;
; we rewrite to bare `@name`. Per-call-site / per-type-reference
; captures (method_invocation, object_creation_expression, superclass)
; are dropped — too noisy for an outline. Added enum / record /
; annotation_type / constructor / field captures that aider's spec
; omits.

(class_declaration
    name: (identifier) @name) @definition.class

(interface_declaration
    name: (identifier) @name) @definition.interface

(enum_declaration
    name: (identifier) @name) @definition.enum

(record_declaration
    name: (identifier) @name) @definition.record

(annotation_type_declaration
    name: (identifier) @name) @definition.annotation

(method_declaration
    name: (identifier) @name) @definition.method

(constructor_declaration
    name: (identifier) @name) @definition.constructor

(field_declaration
    declarator: (variable_declarator
        name: (identifier) @name)) @definition.field
