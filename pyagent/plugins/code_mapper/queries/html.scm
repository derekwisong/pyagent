; Tags query for HTML.
;
; CUSTOM (not vendored): tree-sitter-html upstream ships only
; highlights.scm and injections.scm — no tags.scm. Companion config:
; html.toml.
;
; HTML doesn't have functions/classes; the useful structure is the
; document outline. We capture:
;
;   * Headings h1..h6 — name = the heading text content.
;   * Elements with an `id` attribute — name = the id value, parent =
;     the tag name (so the agent sees `kind=element_id name=intro
;     parent=section`).
;   * Semantic landmarks (nav/main/section/article/aside/header/footer)
;     — name = tag name.
;   * <script> and <style> blocks — name = "script" / "style".
;
; The DOM as a whole is intentionally NOT captured — a real page has
; hundreds of <div>s and the symbol map would be useless. Stick to
; outline-bearing nodes only.

; --- Headings ------------------------------------------------------
; Match elements whose tag name is h1..h6, capture the inner text as
; @name and the element itself as @definition.heading.

(element
    (start_tag
        (tag_name) @_h
        (#match? @_h "^[hH][1-6]$"))
    (text) @name) @definition.heading

; --- Elements with id attribute -----------------------------------
; Capture the value of any id="..." attribute as @name, and the
; element itself as @definition.element_id. Also capture the tag
; name as @parent so the agent sees what kind of element owns the id.

(element
    (start_tag
        (tag_name) @parent
        (attribute
            (attribute_name) @_attr
            (quoted_attribute_value
                (attribute_value) @name)
            (#eq? @_attr "id")))) @definition.element_id

; --- Semantic landmarks --------------------------------------------
; nav, main, section, article, aside, header, footer.
; @name = the tag itself (so search-by-name works).

(element
    (start_tag
        (tag_name) @name
        (#match? @name "^(nav|main|section|article|aside|header|footer)$"))) @definition.section

; --- script / style blocks ----------------------------------------
; tree-sitter-html distinguishes <script> and <style> as their own
; node types so we can target them precisely.

(script_element
    (start_tag
        (tag_name) @name)) @definition.script

(style_element
    (start_tag
        (tag_name) @name)) @definition.style
