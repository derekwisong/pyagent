; Tags query for C.
;
; Source: tree-sitter/tree-sitter-c @ b780e47fc780ddc8da13afa35a3f4ed5c157823d
;   queries/tags.scm — https://github.com/tree-sitter/tree-sitter-c/blob/master/queries/tags.scm
;
; Companion config: c.toml.
;
; DEVIATION FROM UPSTREAM: upstream collapses struct/union/typedef/enum
; under @definition.class / @definition.type. We split them for the
; same reason as Rust — finer kinds are useful for the agent. Patterns
; below replace upstream's collapsed names with kind-specific ones.

(struct_specifier
    name: (type_identifier) @name
    body: (_)) @definition.struct

; Cover both shapes the C grammar produces:
;   `union token foo;`            → declaration → union_specifier
;   `union token { ... };`        → bare union_specifier at translation_unit
(declaration
    type: (union_specifier
        name: (type_identifier) @name)) @definition.union

(union_specifier
    name: (type_identifier) @name
    body: (_)) @definition.union

(function_declarator
    declarator: (identifier) @name) @definition.function

(type_definition
    declarator: (type_identifier) @name) @definition.typedef

(enum_specifier
    name: (type_identifier) @name) @definition.enum
