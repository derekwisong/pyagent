; Tags query for JSON.
;
; CUSTOM (not vendored): JSON has no upstream tags.scm — the grammar
; is too small for a "tags" concept to make sense in the GitHub
; code-nav sense. We capture every `pair` (object key/value) so the
; agent gets an outline of the document's keys at every depth, which
; is the primary navigational structure in a JSON config file.
; Companion config: json.toml.

(pair
    key: (string
        (string_content) @name)) @definition.field
