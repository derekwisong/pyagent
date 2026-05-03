# Skills

Skills are bundles of instructions (and optional helper scripts) the agent
can load on demand. The system prompt advertises *that* a skill exists; the
agent calls `read_skill(<name>)` when its description matches what the user
is asking for, and the skill's body lands in context for the rest of the
session.

## Layout

A skill is a directory with a `SKILL.md`:

```
example-skill/
  SKILL.md          # YAML-ish frontmatter + instructions for the agent
  scripts/          # optional — CLI helpers the agent invokes via the shell tool
    cli.py
```

Frontmatter fields:

| Field | Purpose |
| --- | --- |
| `name` | Catalog identifier the agent uses to load the skill. |
| `description` | One-line summary; the agent uses this to decide relevance. |

Skills don't register Python tools. Helper scripts under `scripts/` are
invoked via the regular shell tool, so they go through the same Bash safety
checks as any other command.

## Discovery order

Later wins; project-local overrides everything:

1. `<package>/skills/<name>/` — bundled with pyagent
2. `<config-dir>/skills/<name>/` — user-installed
3. `./.pyagent/skills/<name>/` — project-local

The catalog re-renders before every model call, so a skill you (or the
agent) just authored shows up on the next call — no restart needed.

## Bundled skills

Bundled skills load directly from the package — no install step, no copy on
disk. Upgrading pyagent updates them for free.

**`write-skill`**, **`write-plugin`**, and **`pdf-from-markdown`** are
enabled out of the box. The rest are opt-in to keep the catalog tight.
Enable additional bundled skills by listing their names in
`<config-dir>/config.toml`:

```toml
built_in_skills_enabled = [
  "write-skill", "write-plugin", "pdf-from-markdown",
  "flight-tracker",
]
```

Setting `built_in_skills_enabled` replaces the default list, so include
every bundled skill you want available.

Run `pyagent-skills list` to see each bundled skill's name, description,
and current `[enabled]` / `[disabled]` state.

| Skill | What it does |
| --- | --- |
| `write-skill` | Authoring guide — load this when you want the agent to write a new skill for you. **Enabled by default.** |
| `write-plugin` | Authoring guide for plugins — manifest schema, PluginAPI surface, hooks. Load when creating or modifying a plugin. **Enabled by default.** |
| `pdf-from-markdown` | Convert markdown to PDF using pandoc. Markdown-only skill (no scripts). **Enabled by default.** |
| `aviation-weather` | METARs, TAFs, PIREPs, AFD, AIRMETs/SIGMETs around an airport. Uses aviationweather.gov; no key needed. |
| `flight-tracker` | Live aircraft state vectors near a point or by ICAO24 hex via OpenSky. Anonymous works; OAuth2 client credentials unlock more. |
| `faa-registry` | Look up FAA aircraft registry records (US tail numbers) by N-number, owner, or make/model. |

## Customizing or removing

To customize a bundled skill, copy its directory into `<config-dir>/skills/`
or `./.pyagent/skills/` and edit. The override takes precedence regardless
of `built_in_skills_enabled` — user-installed and project-local tiers are
never gated by config.

`pyagent-skills uninstall <name>` removes a user- or project-local copy.
Bundled skills can't be uninstalled (they ship with the package); to keep
one out of the catalog, just leave it out of `built_in_skills_enabled`.

## Authoring a skill

Tell the agent to load `write-skill` and ask it to author a new skill for
you. Or write `SKILL.md` by hand following the layout above.
