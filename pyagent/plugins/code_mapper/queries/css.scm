; Tags query for CSS.
;
; CUSTOM (not vendored): no upstream tags.scm. Useful structure for
; agents reading a stylesheet is the selector list — what rules
; exist, by class / id / tag — and the @media / @keyframes / @font-face
; at-rules. We capture top-level selectors and at-rule headers.
; Companion config: css.toml.

(class_selector
    (class_name
        (identifier) @name)) @definition.class_selector

(id_selector
    (id_name) @name) @definition.id_selector

; @media (min-width: 600px) { ... }
(media_statement
    "@media" @name) @definition.at_rule

; @keyframes spin { ... } — keyframes_name is positional, not a field.
(keyframes_statement
    (keyframes_name) @name) @definition.at_rule

; @font-face { ... } and other at-rule headers.
(at_rule
    (at_keyword) @name) @definition.at_rule
