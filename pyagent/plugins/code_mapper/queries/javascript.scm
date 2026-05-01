; Tags query for JavaScript.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/javascript-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/javascript-tags.scm
; License: Apache-2.0
;
; Companion config: javascript.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`
; and ship `(#strip!)` / `(#select-adjacent!)` predicates for doc-
; comment association. Our loader expects bare `@name` and doesn't
; implement those predicates, so captures are simplified and the
; @doc / predicate scaffolding is dropped. Per-call-site
; @reference.call captures are also dropped (excessive noise for
; symbol outlines; aider uses them for repo-map ranking).

(method_definition
    name: (property_identifier) @name) @definition.method

(class
    name: (_) @name) @definition.class

(class_declaration
    name: (_) @name) @definition.class

(function_expression
    name: (identifier) @name) @definition.function

(function_declaration
    name: (identifier) @name) @definition.function

(generator_function
    name: (identifier) @name) @definition.function

(generator_function_declaration
    name: (identifier) @name) @definition.function

; Top-level `const x = () => ...` / `var x = function() {}` shapes
; — the modern idiom for "this is a function" in modules.
(lexical_declaration
    (variable_declarator
        name: (identifier) @name
        value: [(arrow_function) (function_expression)])) @definition.function

(variable_declaration
    (variable_declarator
        name: (identifier) @name
        value: [(arrow_function) (function_expression)])) @definition.function

; Object-literal method shorthand: `{ key: () => {} }`.
(pair
    key: (property_identifier) @name
    value: [(arrow_function) (function_expression)]) @definition.function
