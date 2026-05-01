; Tags query for Ruby.
;
; Source: Aider-AI/aider @ 3ec8ec5a7d695b08a6c24fe6c0c235c8f87df9af
;   aider/queries/tree-sitter-language-pack/ruby-tags.scm
;   https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-language-pack/ruby-tags.scm
; License: Apache-2.0
;
; Companion config: ruby.toml.
;
; DEVIATION FROM UPSTREAM: aider's queries use `@name.definition.X`
; with `(#strip!)`/`(#select-adjacent!)` doc-comment predicates and
; per-identifier `@reference.call` captures (using `(#is-not? local)`,
; which our loader does not implement). Captures are rewritten to
; bare `@name` and the @doc/predicate scaffolding plus the noisy
; reference patterns are dropped.

(method
    name: (_) @name) @definition.method

(singleton_method
    name: (_) @name) @definition.method

(alias
    name: (_) @name) @definition.method

(class
    name: [
        (constant) @name
        (scope_resolution
            name: (_) @name)
    ]) @definition.class

(singleton_class
    value: [
        (constant) @name
        (scope_resolution
            name: (_) @name)
    ]) @definition.class

(module
    name: [
        (constant) @name
        (scope_resolution
            name: (_) @name)
    ]) @definition.module
