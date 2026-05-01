; Tags query for Swift.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/swift-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/swift-tags.scm
; License: Apache-2.0
;
; Companion config: swift.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`
; (rewritten to bare `@name` here) and emit duplicate symbols for
; in-class function_declarations (once via the class_body wrapper as
; @definition.method, once via the bare function_declaration pattern
; as @definition.function). We drop the wrapper patterns and rely on
; swift.toml's promote rule to upgrade in-class functions to methods,
; matching the C++ approach.

(class_declaration
    name: (type_identifier) @name) @definition.class

(protocol_declaration
    name: (type_identifier) @name) @definition.interface

(protocol_function_declaration
    name: (simple_identifier) @name) @definition.method

(property_declaration
    (pattern (simple_identifier) @name)) @definition.property

(function_declaration
    name: (simple_identifier) @name) @definition.function
