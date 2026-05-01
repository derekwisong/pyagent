; Tags query for SQL.
;
; CUSTOM (not vendored): no upstream tags.scm. The useful structure
; for an agent reading SQL is the schema-defining statements
; (CREATE TABLE / VIEW / INDEX / FUNCTION / PROCEDURE / TRIGGER /
; SCHEMA). Per-SELECT references are intentionally omitted — they
; would dwarf real definitions in any non-trivial query file.
; Companion config: sql.toml.

(create_table
    (object_reference
        name: (identifier) @name)) @definition.table

(create_view
    (object_reference
        name: (identifier) @name)) @definition.view

; create_index puts the index name as a positional `column:` field on
; the create_index node itself, not inside an object_reference.
(create_index
    column: (identifier) @name) @definition.index

(create_function
    (object_reference
        name: (identifier) @name)) @definition.function

(create_procedure
    (object_reference
        name: (identifier) @name)) @definition.procedure

; create_trigger has multiple object_references (the trigger name,
; the target table, optionally the executed function). The trigger
; name is the one DIRECTLY after `keyword_trigger`; we match that
; via positional anchoring with the keyword_trigger immediately
; preceding.
(create_trigger
    (keyword_trigger)
    .
    (object_reference
        name: (identifier) @name)) @definition.trigger

; create_schema's name is a bare identifier child.
(create_schema
    (identifier) @name) @definition.schema
