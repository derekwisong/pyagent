; Tags query for TOML.
;
; CUSTOM (not vendored): TOML has no upstream tags.scm. We capture
; section headers (`[section]` and `[[array.of.tables]]`) as modules
; and pairs as fields. The mapper attributes child fields to their
; enclosing section via parent-walk. Companion config: toml.toml.

; [section] header — bare_key directly under a `table` parent.
(table
    (bare_key) @name) @definition.module

; [section.subsection] dotted-key header — capture the whole
; dotted_key span as the name.
(table
    (dotted_key) @name) @definition.module

; [[array.of.tables]] header (and its dotted variant).
(table_array_element
    (bare_key) @name) @definition.module

(table_array_element
    (dotted_key) @name) @definition.module

; Top-level or in-section key/value pairs.
(pair
    (bare_key) @name) @definition.field

; Dotted/quoted keys (`a.b.c = 1`, `"key" = 1`).
(pair
    (dotted_key) @name) @definition.field

(pair
    (quoted_key) @name) @definition.field
