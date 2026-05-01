; Tags query for Rust.
;
; Source: tree-sitter/tree-sitter-rust @ 77a3747266f4d621d0757825e6b11edcbf991ca5
;   queries/tags.scm — https://github.com/tree-sitter/tree-sitter-rust/blob/master/queries/tags.scm
;
; Companion config: rust.toml.
;
; DEVIATION FROM UPSTREAM: upstream collapses struct/enum/union/type-alias
; under @definition.class so a generic tags consumer renders them all as
; "class". We keep them distinct (`struct`/`enum`/`union`/`type`) because
; the agent benefits from the finer kind. The upstream patterns are
; commented out below for traceability and replaced with kind-specific
; capture names.

; --- ADTs ------------------------------------------------------------
; upstream: (struct_item name: (type_identifier) @name) @definition.class
(struct_item
    name: (type_identifier) @name) @definition.struct

; upstream: (enum_item name: (type_identifier) @name) @definition.class
(enum_item
    name: (type_identifier) @name) @definition.enum

; upstream: (union_item name: (type_identifier) @name) @definition.class
(union_item
    name: (type_identifier) @name) @definition.union

; upstream: (type_item name: (type_identifier) @name) @definition.class
(type_item
    name: (type_identifier) @name) @definition.type

; --- Functions / methods --------------------------------------------
; DEVIATION FROM UPSTREAM: upstream emits @definition.method for any
; function_item inside a declaration_list, but that incorrectly tags
; functions inside `mod { ... }` blocks (which also use
; declaration_list as their body) as methods. We instead capture all
; function_items as @definition.function and let rust.toml's
; promote-rules upgrade to "method" only when the enclosing definition
; is impl_item or trait_item.
(function_item
    name: (identifier) @name) @definition.function

; --- Traits, modules, macros ----------------------------------------
(trait_item
    name: (type_identifier) @name) @definition.trait

(mod_item
    name: (identifier) @name) @definition.module

(macro_definition
    name: (identifier) @name) @definition.macro

; --- References (call sites) ---------------------------------------
(call_expression
    function: (identifier) @name) @reference.call

(call_expression
    function: (field_expression
        field: (field_identifier) @name)) @reference.call

(macro_invocation
    macro: (identifier) @name) @reference.call

; --- impl blocks ----------------------------------------------------
; Upstream emits @reference.implementation; we promote to @definition.impl
; because for code-mapping purposes these blocks ARE definition sites
; (where methods get attached to a type).
(impl_item
    trait: (type_identifier) @name) @definition.impl

(impl_item
    type: (type_identifier) @name
    !trait) @definition.impl
