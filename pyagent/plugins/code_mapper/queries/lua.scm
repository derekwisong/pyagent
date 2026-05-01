; Tags query for Lua.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/lua-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/lua-tags.scm
; License: Apache-2.0
;
; Companion config: lua.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`;
; we rewrite to bare `@name`. The function_call reference patterns
; are dropped — too noisy for a symbol outline.

; Plain `function foo()` and `function t.foo()` (dot-indexed
; assignment to a table field).
(function_declaration
    name: [
        (identifier) @name
        (dot_index_expression
            field: (identifier) @name)
    ]) @definition.function

; Method shorthand: `function obj:method()`.
(function_declaration
    name: (method_index_expression
        method: (identifier) @name)) @definition.method

; `local f = function() ... end` and `t.f = function() ... end`.
(assignment_statement
    (variable_list
        name: [
            (identifier) @name
            (dot_index_expression
                field: (identifier) @name)
        ])
    (expression_list
        value: (function_definition))) @definition.function

; Table-literal entries that bind a function: `{ key = function() end }`.
(table_constructor
    (field
        name: (identifier) @name
        value: (function_definition))) @definition.function
