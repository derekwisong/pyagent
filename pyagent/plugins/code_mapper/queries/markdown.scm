; Tags query for Markdown.
;
; CUSTOM (not vendored): no upstream tags.scm. The useful structure
; for an agent navigating a markdown doc is the heading hierarchy
; (H1, H2, H3), not body text. We capture only ATX-style headings
; (`#`, `##`, `###`); H4-H6 are intentionally skipped to keep the
; outline scannable in long docs. Companion config: markdown.toml.

(atx_heading
    (atx_h1_marker)
    heading_content: (inline) @name) @definition.heading

(atx_heading
    (atx_h2_marker)
    heading_content: (inline) @name) @definition.heading

(atx_heading
    (atx_h3_marker)
    heading_content: (inline) @name) @definition.heading
