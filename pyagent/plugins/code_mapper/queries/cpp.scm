; Tags query for C++.
;
; Source: tree-sitter/tree-sitter-cpp @ 8b5b49eb196bec7040441bee33b2c9a4838d6967
;   queries/tags.scm — https://github.com/tree-sitter/tree-sitter-cpp/blob/master/queries/tags.scm
;
; Companion config: cpp.toml.
;
; DEVIATION FROM UPSTREAM: same kind-splitting as C/Rust (struct vs
; class vs enum vs union vs typedef instead of upstream's collapsed
; @definition.class / @definition.type), plus added namespace capture.

(struct_specifier
    name: (type_identifier) @name
    body: (_)) @definition.struct

(class_specifier
    name: (type_identifier) @name) @definition.class

(declaration
    type: (union_specifier
        name: (type_identifier) @name)) @definition.union

(union_specifier
    name: (type_identifier) @name
    body: (_)) @definition.union

(enum_specifier
    name: (type_identifier) @name) @definition.enum

(type_definition
    declarator: (type_identifier) @name) @definition.typedef

(namespace_definition
    name: (namespace_identifier) @name) @definition.namespace

; Plain function (free function or method body inside a class body).
; Methods inside class_specifier are upgraded to "method" via
; cpp.toml's promote rules; out-of-line `Foo::bar` definitions are
; covered by the qualified pattern below.
(function_declarator
    declarator: (identifier) @name) @definition.function

(function_declarator
    declarator: (field_identifier) @name) @definition.function

; Out-of-line method definitions: `void Foo::bar() { ... }`.
; Captures @name = "bar" and @parent = "Foo" — the @parent capture
; overrides the AST-walk-based parent attribution (which would
; otherwise resolve to the surrounding namespace).
(function_declarator
    declarator: (qualified_identifier
        scope: (namespace_identifier) @parent
        name: (identifier) @name)) @definition.method
