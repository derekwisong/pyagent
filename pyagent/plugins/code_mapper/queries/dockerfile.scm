; Tags query for Dockerfile.
;
; CUSTOM (not vendored): no upstream tags.scm. Useful structure for
; an agent reading a Dockerfile is the instruction list — what does
; each line do — and the stage names in multi-stage builds. We emit
; one symbol per instruction with kind=directive (name=instruction
; keyword like "RUN" / "COPY"), plus a kind=stage symbol per
; `FROM ... AS <name>` for quick stage navigation. Companion config:
; dockerfile.toml.

; Stage marker: `FROM image AS my-stage`. Capture the alias as @name
; and the whole instruction as the def — agents use this to navigate
; multi-stage builds.
(from_instruction
    as: (image_alias) @name) @definition.stage

; FROM without AS still gets a directive entry (named "FROM").
(from_instruction
    "FROM" @name) @definition.directive

; Each instruction type's keyword is an anonymous token; capture it
; literally as @name. Per-instruction patterns rather than one big
; alternation so a future grammar that adds a new instruction doesn't
; silently break the query.
(run_instruction "RUN" @name) @definition.directive
(copy_instruction "COPY" @name) @definition.directive
(workdir_instruction "WORKDIR" @name) @definition.directive
(env_instruction "ENV" @name) @definition.directive
(expose_instruction "EXPOSE" @name) @definition.directive
(cmd_instruction "CMD" @name) @definition.directive
(entrypoint_instruction "ENTRYPOINT" @name) @definition.directive
(label_instruction "LABEL" @name) @definition.directive
(user_instruction "USER" @name) @definition.directive
(arg_instruction "ARG" @name) @definition.directive
(volume_instruction "VOLUME" @name) @definition.directive
(shell_instruction "SHELL" @name) @definition.directive
(onbuild_instruction "ONBUILD" @name) @definition.directive
(healthcheck_instruction "HEALTHCHECK" @name) @definition.directive
(add_instruction "ADD" @name) @definition.directive
(maintainer_instruction "MAINTAINER" @name) @definition.directive
