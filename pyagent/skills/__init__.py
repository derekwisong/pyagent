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

Resolution order (first wins, local overrides everything):
  1. ./.pyagent/skills/<name>/SKILL.md       — project-local
  2. <config-dir>/skills/<name>/SKILL.md     — user-installed

Bundled skills live at `pyagent/skills/<name>/` inside the package.
They are normally opt-in via `pyagent-skills install`, with one
exception: a bundled skill whose frontmatter sets `auto_install: true`
is seeded into `<config-dir>/skills/<name>/` on first run.

Auto-install is tracked in `<config-dir>/skills/.auto_installed`,
which lists the bundled skill names that have already been seeded.
On launch, any flagged bundled skill *not* in that file gets copied
in (and added to the file). Uninstalling a seeded skill removes the
directory but leaves the line — so it stays uninstalled across
restarts. To opt back in, either rerun `pyagent-skills install
<name>` or delete the matching line from `.auto_installed`.

Note: `.auto_installed` lives in the user's writable config dir, so
the agent can technically modify it through its normal file/shell
tools. The marker is a convention to keep auto-install behavior
predictable, not a security boundary.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from pyagent import paths

logger = logging.getLogger(__name__)

LOCAL_SKILLS_DIR = Path(".pyagent") / "skills"
PACKAGE_SKILLS_PKG = "pyagent.skills"
_AUTO_INSTALLED_MARKER = ".auto_installed"


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


_AUTO_INSTALLED_HEADER = (
    "# Bundled skills that have been auto-seeded into this directory.\n"
    "# Each line is a skill name. A name listed here is treated as\n"
    "# already-handled and will NOT be re-seeded on subsequent runs,\n"
    "# even if the matching directory is removed. Delete a line to\n"
    "# opt that skill back into auto-install on next launch.\n"
)


def _read_auto_installed(target_root: Path) -> set[str]:
    marker = target_root / _AUTO_INSTALLED_MARKER
    if not marker.exists():
        return set()
    try:
        text = marker.read_text()
    except OSError as e:
        logger.warning("could not read %s: %s", marker, e)
        return set()
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _write_auto_installed(target_root: Path, names: set[str]) -> None:
    marker = target_root / _AUTO_INSTALLED_MARKER
    target_root.mkdir(parents=True, exist_ok=True)
    body = "\n".join(sorted(names))
    marker.write_text(_AUTO_INSTALLED_HEADER + body + ("\n" if body else ""))


def _seed_auto_install_skills() -> None:
    """Seed bundled skills with `auto_install: true` into the user's
    config-dir skills folder.

    A skill is seeded the first time it's seen. After that, its name
    is recorded in `<config-dir>/skills/.auto_installed` and the
    seeder will skip it forever — even if the destination directory
    has been deleted via `pyagent-skills uninstall`. Removing the
    line from `.auto_installed` (or running `pyagent-skills install`
    manually) is how a user opts back in.

    Errors are logged and swallowed — a broken seed must never block
    discovery of the user's other skills.
    """
    try:
        bundled = _bundled_root()
    except (ModuleNotFoundError, FileNotFoundError):
        return
    if not bundled.exists():
        return
    target_root = paths.config_dir() / "skills"
    seeded = _read_auto_installed(target_root)
    changed = False
    for skill_md in bundled.glob("*/SKILL.md"):
        try:
            fm, _ = _parse_frontmatter(skill_md.read_text())
        except OSError as e:
            logger.warning("auto_install scan: %s unreadable: %s", skill_md, e)
            continue
        if fm.get("auto_install", "").strip().lower() != "true":
            continue
        name = fm.get("name") or skill_md.parent.name
        if name in seeded:
            continue
        dest = target_root / name
        if not dest.exists():
            try:
                target_root.mkdir(parents=True, exist_ok=True)
                shutil.copytree(skill_md.parent, dest)
            except OSError as e:
                logger.warning("auto_install seed of %s failed: %s", name, e)
                continue
            logger.info("seeded bundled skill %s -> %s", name, dest)
        seeded.add(name)
        changed = True
    if changed:
        try:
            _write_auto_installed(target_root, seeded)
        except OSError as e:
            logger.warning(
                "could not update %s: %s", target_root / _AUTO_INSTALLED_MARKER, e
            )


def discover() -> dict[str, Skill]:
    """Discover all installed skills. Local overrides user."""
    _seed_auto_install_skills()
    skills = _scan_dir(paths.config_dir() / "skills")
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
