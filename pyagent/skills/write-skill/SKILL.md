---
name: write-skill
description: Author a new pyagent skill — directory layout, SKILL.md frontmatter, helper scripts, and where to put it. Load this when the user asks you to create a skill.
---

# Writing a pyagent skill

A skill is a folder with a `SKILL.md`. The agent's catalog (in the
system prompt) lists installed skills by name + description; calling
`read_skill(<name>)` returns that folder's instructions to the agent.
Skills can ship executable scripts alongside SKILL.md — the agent
invokes them through the regular shell tool, not through any special
registration mechanism.

## Directory layout

    <root>/<skill-name>/
        SKILL.md          required
        scripts/          optional — bundled CLI helpers
            cli.py
        references/       optional — long-form docs the body links to

The skill name is the directory name and must match the `name:` field
in the frontmatter. Hyphens are fine (`pdf-extract`, `git-cleanup`).

## Where to put `<root>`

Pick based on scope. Discovery is three-tier; later tiers override
earlier ones:

- `<package>/skills/` — **bundled** with pyagent itself. Read-only
  from the agent's perspective; you don't author here. Updated by
  upgrading the pyagent install. Bundled skills are gated by
  `built_in_skills_enabled` in `<config-dir>/config.toml` — only
  listed ones load. (Skills you author land in one of the two tiers
  below, which are never gated.)
- `<config-dir>/skills/` — **user-wide**. Available across every
  project the user runs pyagent in. The `<config-dir>` is OS-dependent
  (`~/.config/pyagent/` on Linux,
  `~/Library/Application Support/pyagent/` on macOS).
- `./.pyagent/skills/` — **project-local**. Lives in the user's working
  directory. Overrides user-wide and bundled skills of the same name.

A user-wide or project-local skill with the same name as a bundled
one shadows the bundled copy entirely — that's how customization
works: copy the bundled directory to one of the override roots and
edit there. The override loads regardless of whether the bundled
skill is in `built_in_skills_enabled`.

If you don't know which the user wants, ask once. Don't guess on
behalf of "future projects".

## SKILL.md frontmatter

The file starts with a `---`-delimited block:

    ---
    name: skill-name
    description: One-line summary used to decide relevance. Be specific about what the skill does and when to use it — this is all the agent sees in the catalog before deciding to load it.
    ---

Fields:

- **name** (required) — Identifier the agent uses with `read_skill(...)`.
  Must match the directory name.
- **description** (required) — One sentence. The agent reads this to
  decide whether to call `read_skill`. Lead with the verb, name the
  domain. Bad: "Helper for PDF stuff." Good: "Extract text or tables
  from a PDF file on disk. Use when the user provides a .pdf path and
  wants its contents."

There is no `tools:` field. Skills don't register Python functions as
agent tools — they ship CLI scripts the agent runs via the shell tool.

## Body

After the frontmatter, write markdown that becomes the skill's
instructions. Address the agent directly. Cover:

- What the skill is for and when to load it.
- The bundled scripts (if any) — exact subcommand syntax and what
  each prints. The agent invokes them with the shell tool, so be
  explicit about flags and argument order.
- Domain-specific gotchas, abbreviations, expected inputs.
- Onboarding hints if the skill has a credentialed tier (suggest the
  user set up env vars when they hit a limit, etc.).

Don't repeat the description; the agent already has it. Do focus on
what *future-you* will need: concrete examples, codes to translate,
where the data is best-effort.

When the agent loads a skill, the body is prefixed with a header like
`_Skill loaded from `/abs/path/to/<skill-name>`._` so the agent knows
the absolute path of the directory and can construct script
invocations like `python /abs/path/to/<skill-name>/scripts/cli.py ...`.
Reference scripts in your body using the `<skill_dir>/scripts/...`
shorthand and let that header bind it to a concrete path.

## scripts/ (optional)

If the skill ships helpers, put them under `scripts/`. The convention
is one `cli.py` per skill that uses argparse subcommands — clean for
skills with several distinct operations, and the agent can always run
`python cli.py --help` to recover syntax. Multiple separate scripts
are fine too if each is independently meaningful.

Conventions:

- Plain Python with a `#!/usr/bin/env python3` shebang and an
  `if __name__ == "__main__":` block.
- Print results to stdout. Return exit code 0 on success, non-zero on
  unrecoverable failure.
- For predictable failures (bad input, no records found, rate
  limited), still exit 0 but write a clear `<...>` marker line — the
  agent reads stdout and does better with structured failure data
  than with stderr/exit-code parsing.
- The agent runs scripts through pyagent's normal `execute` shell
  tool, which inherits pyagent's environment. So if pyagent runs in a
  venv with `requests` installed, `python <script>` finds it.
- No code runs at skill activation — only when the agent invokes the
  shell. There is no per-session "approve loading code" prompt.

## Activation lifecycle

1. Before every model call, pyagent rescans all three roots (bundled,
   user, project-local) and rebuilds the catalog injected into the
   system prompt. A skill authored mid-session shows up on the next
   inner call — no restart needed.
2. When the agent calls `read_skill(<name>)`, the body is returned as
   the tool result, prefixed with the skill's resolved directory
   path. The lookup also reads from the live registry, so a freshly
   added skill is immediately loadable.
3. The agent then invokes bundled scripts via the shell tool. No
   imports, no per-session approval prompt — scripts run when (and
   only when) the agent invokes the shell.

## Recipe: writing a skill end-to-end

When the user asks for a new skill, walk through:

1. **Pick a name and scope.** Confirm with the user: project-local or
   user-wide. Default to project-local unless they say otherwise.
2. **Make the directory** with the appropriate root, plus `scripts/`
   if helpers are needed.
3. **Write SKILL.md**: frontmatter (name, description), then the
   body. Keep the body focused — instructions, not a manual.
4. **Write `scripts/cli.py`** (if applicable). Argparse subcommands,
   stdout output, executable-style entry point.
5. **No restart needed** — the next inner call's catalog rescan will
   pick up the new skill. Mention the skill to the user so they know
   it's available.
6. If the user wants to ship it as a built-in: copy the directory
   under `pyagent/skills/` in the source tree. That's a developer-side
   workflow, not something to do from the agent.
