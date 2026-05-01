; Tags query for YAML.
;
; CUSTOM (not vendored): YAML has no upstream tags.scm — the grammar
; is too small for a "tags" concept. We capture mapping keys as
; fields, which is the document outline for any config-shaped YAML
; file (Kubernetes manifests, GitHub Actions workflows, Ansible
; playbooks, docker-compose, etc.). Companion config: yaml.toml.

(block_mapping_pair
    key: (flow_node
        (plain_scalar
            (string_scalar) @name))) @definition.field

; Quoted-string keys (`"key": value`) parse as a different shape.
(block_mapping_pair
    key: (flow_node
        (double_quote_scalar) @name)) @definition.field

(block_mapping_pair
    key: (flow_node
        (single_quote_scalar) @name)) @definition.field
