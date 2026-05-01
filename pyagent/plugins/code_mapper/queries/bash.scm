; Tags query for Bash.
;
; CUSTOM (not vendored): no upstream tags.scm exists for bash —
; tree-sitter-bash ships highlights but not tags, and aider /
; nvim-treesitter / tree-sitter-bash itself don't ship one. Companion
; config: bash.toml.
;
; Bash's only meaningful "definition" is `function foo() { ... }`
; or the equivalent `foo() { ... }` shorthand. We also capture
; top-level variable assignments (KEY=value) since those are the
; primary thing a shell-script outline cares about — they're the
; configurable knobs.

; Both `function foo { }` and `foo() { }` parse as function_definition
; with name: word.
(function_definition
    name: (word) @name) @definition.function

; Top-level variable assignments: KEY=value or KEY="..."
; The grammar wraps every assignment in variable_assignment regardless
; of nesting, so this matches every assignment in the file. That's
; arguably noisy in a deeply nested script, but in practice shell
; scripts have shallow nesting.
(variable_assignment
    name: (variable_name) @name) @definition.variable
