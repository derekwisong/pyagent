"""Skills: bundles of instructions (and optional helper scripts) the
agent loads on demand.

Two-stage pattern. The system prompt advertises *that* a skill exists
(name + description). The agent calls `read_skill(name)` to actually
load the body into context.

A skill is a directory with a `SKILL.md` whose frontmatter declares:

    ---
    name: example
    description: One-line summary the agent uses to decide relevance.
    ---

Skills can ship executable scripts alongside SKILL.md (typically under
`scripts/`); the body teaches the agent how to invoke them via the
existing shell tool. Nothing is imported into the agent's process —
each script call is a subprocess, gated by the same Bash safety as
any other shell command.

Resolution order (later wins, so project-local overrides everything):
  1. <package>/skills/<name>/SKILL.md         — bundled with pyagent
  2. <config-dir>/skills/<name>/SKILL.md      — user-installed
  3. ./.pyagent/skills/<name>/SKILL.md        — project-local

Bundled skills load directly from the installed package. Upgrading
pyagent updates them for free. To customize a bundled skill, copy its
directory into the user-installed or project-local root and edit
there — the override takes precedence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pyagent import config, paths

logger = logging.getLogger(__name__)

LOCAL_SKILLS_DIR = Path(".pyagent") / "skills"
PACKAGE_SKILLS_PKG = "pyagent.skills"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: Path  # absolute path to SKILL.md


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse leading `---`-delimited YAML-ish frontmatter.

    Supports flat `key: value` lines only — that's all skill metadata
    needs, and it spares us a yaml dependency.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + len("\n---\n") :].lstrip("\n")
    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        fm[key.strip()] = val.strip()
    return fm, body


def _load_skill(skill_md: Path) -> Skill | None:
    try:
        text = skill_md.read_text()
    except OSError as e:
        logger.warning("skill %s unreadable: %s", skill_md, e)
        return None
    fm, body = _parse_frontmatter(text)
    name = fm.get("name") or skill_md.parent.name
    description = fm.get("description")
    if not description:
        logger.warning("skill %s missing description, skipping", skill_md)
        return None
    return Skill(
        name=name,
        description=description,
        body=body,
        source=skill_md.resolve(),
    )


def _scan_dir(root: Path) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    if not root.exists():
        return found
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill = _load_skill(skill_md)
        if skill:
            found[skill.name] = skill
    return found


def _bundled_root() -> Path:
    return Path(str(resources.files(PACKAGE_SKILLS_PKG)))


def _enabled_bundled_names() -> set[str]:
    """Names of built-in skills the user has opted into via config.toml."""
    cfg = config.load()
    raw = cfg.get("built_in_skills_enabled", [])
    if not isinstance(raw, list):
        logger.warning("config.built_in_skills_enabled is not a list; ignoring")
        return set()
    return {n for n in raw if isinstance(n, str)}


def discover() -> dict[str, Skill]:
    """Discover all skills across the three tiers. Later tiers win.

    Bundled skills are filtered against `built_in_skills_enabled` in
    config.toml — only explicitly enabled ones appear. User-installed
    and project-local tiers are unfiltered (their presence on disk is
    the enablement).
    """
    bundled = _scan_dir(_bundled_root())
    enabled = _enabled_bundled_names()
    skills = {n: s for n, s in bundled.items() if n in enabled}
    skills.update(_scan_dir(paths.config_dir() / "skills"))
    skills.update(_scan_dir(LOCAL_SKILLS_DIR))
    return skills


def catalog(skills: dict[str, Skill]) -> str:
    """Render the catalog block injected into the system prompt."""
    if not skills:
        return ""
    lines = [
        "## Available skills",
        "",
        "Each line is a skill you can load on demand. When the user's "
        "request matches a description, call `read_skill(<name>)` to "
        "load that skill's instructions. The call is idempotent — if a "
        "long session has pushed the body out of working memory or you "
        "are unsure of a script's exact syntax, just call it again.",
        "",
        "This list re-renders before every model call, so a skill you "
        "or the user just installed shows up on the next call — no "
        "session restart needed. The body still requires `read_skill` "
        "to load.",
        "",
    ]
    for skill in sorted(skills.values(), key=lambda s: s.name):
        lines.append(f"- **{skill.name}**: {skill.description}")
    return "\n".join(lines)


def live_catalog() -> str:
    """Discover skills now and render the catalog. Re-runs filesystem
    discovery on each call, so a skill installed or authored
    mid-session appears on the next render."""
    return catalog(discover())


def read_skill(name: str) -> str:
    """Load a skill's instructions into context.

    Idempotent: calling it again on the same skill simply re-injects
    the body, which is the right move if a long session has pushed
    the original load out of attention or you're unsure of a
    script's exact syntax. Any helper scripts the skill ships with
    live alongside its SKILL.md and are invoked via the shell tool.

    Args:
        name: Skill name, exactly as listed in the catalog.

    Returns:
        The skill's instructional body, prefixed with the absolute
        path of the skill's directory. Returns an error marker if
        no such skill is installed.
    """
    skills = discover()
    skill = skills.get(name)
    if not skill:
        available = ", ".join(sorted(skills)) or "(none installed)"
        return f"<unknown skill: {name!r}; available: {available}>"
    skill_dir = skill.source.parent
    header = (
        f"_Skill loaded from `{skill_dir}`. Bundled scripts (if any) "
        f"live under that directory; invoke them with the shell tool._\n\n"
    )
    return header + skill.body
