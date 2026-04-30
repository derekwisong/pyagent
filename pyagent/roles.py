"""Named subagent roles loaded from markdown files.

A role is `name -> (model, description, optional persona, optional
tool allowlist, meta-tool gate)`. Roles let an orchestrator address a
subagent by purpose ("researcher", "reviewer") instead of by raw
provider/model string, and centralize cost/capability decisions in
files the user can drop into a directory without learning TOML.

Three-tier discovery (later wins on case/dash/underscore-normalized name
collision), mirroring skills/plugins/config:

  1. <package>/roles/*.md          — bundled with pyagent
  2. <config-dir>/roles/*.md       — user library (per-machine)
  3. ./.pyagent/roles/*.md         — project-local

Each `.md` file is one role. Filename → role name (`.md` stripped,
dashes and underscores both accepted, normalized to underscores,
lower-cased for lookup; original filename preserved for `path` /
`show`). Optional Hugo-style TOML frontmatter between `+++` delimiters
declares structured fields; the body after the closing fence is the
role's persona prose.

    +++
    model = "anthropic/claude-haiku-4-5"
    tools = ["read_file", "grep", "list_directory", "fetch_url"]
    meta_tools = false
    description = "Cheap reader for triage and summarization."
    +++

    # Role: Researcher

    You are a research specialist. Given a question and a starting
    URL, you fetch, summarize, and cross-reference. ...

All frontmatter fields are optional. Defaults:
  - `model`     — empty string (subagent inherits caller's model).
  - `tools`     — None (inherit the default tool set).
  - `meta_tools` — True (current default; subagent can fan out).
  - `description` — auto-derived from the first non-empty paragraph
    after a leading `# Heading` line, collapsed whitespace, capped at
    ~200 chars. If no body, falls back to the role name.

Backward compat: `[models.<name>]` tables in config.toml still load
with the previous schema, but emit a one-time deprecation warning at
startup pointing at `pyagent-roles migrate`. File-based roles win on
collision (newer authoring surface).
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pyagent import config as config_mod
from pyagent import llms, paths

logger = logging.getLogger(__name__)

LOCAL_ROLES_DIR = Path(".pyagent") / "roles"
PACKAGE_ROLES_PKG = "pyagent.roles_bundled"

_DESCRIPTION_CAP = 200


@dataclass(frozen=True)
class Role:
    """Resolved role definition.

    Attributes:
        name: Canonical (normalized) role identifier — lowercase, with
            underscores. Looked up case-insensitively.
        model: Resolved provider/model string, or "" to mean "inherit
            the caller's model" at spawn time.
        description: One-line summary the orchestrator uses to decide
            when to spawn this role. Renders into the role catalog.
        system_prompt: Default subagent persona body. May be empty.
            Layered onto SOUL/TOOLS/PRIMER, *before* the spawn-time
            task body.
        tools: If non-None, the explicit tool allowlist for this role.
            None means inherit the default tool set.
        meta_tools: Whether this role's subagent can itself spawn
            further subagents. Default True (matches root behavior).
        source: Absolute path the role was loaded from, or None for
            roles defined in config.toml (legacy `[models.<name>]`).
    """

    name: str
    model: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None
    meta_tools: bool
    source: Path | None = None


# ---- Name normalization ---------------------------------------------


def _normalize_name(raw: str) -> str:
    """Canonicalize a role name: lowercase, dashes → underscores."""
    return raw.replace("-", "_").lower()


# ---- Frontmatter parsing --------------------------------------------


_FRONTMATTER_RE = re.compile(
    r"\A\+\+\+[ \t]*\r?\n(.*?)\r?\n\+\+\+[ \t]*\r?\n?",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Strip Hugo-style `+++` TOML frontmatter from `text`.

    Returns (frontmatter_dict, body). On any malformed frontmatter
    (bad TOML, no closing fence) returns ({}, text) and lets the body
    speak for itself. Logs a warning so authors notice.
    """
    if not text.startswith("+++"):
        return {}, text
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # Has opening +++ but no closing one — let the prose render.
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():].lstrip("\n")
    try:
        fm = tomllib.loads(fm_text)
    except tomllib.TOMLDecodeError as e:
        logger.warning("role frontmatter is not valid TOML (%s); ignoring", e)
        return {}, body
    if not isinstance(fm, dict):
        return {}, body
    return fm, body


# ---- Description auto-derivation ------------------------------------


_HEADING_RE = re.compile(r"^\s*#+\s.*$", re.MULTILINE)


def _derive_description(name: str, body: str) -> str:
    """Pull a one-line summary out of the body.

    Strategy: drop a leading `# Heading` line if present, take the
    first non-empty paragraph, collapse whitespace, cap at
    `_DESCRIPTION_CAP` chars. Falls back to the role name if there's
    no body content to mine.
    """
    if not body.strip():
        return name
    lines = body.splitlines()
    # Skip a leading heading (or chain of blank lines + heading).
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and lines[i].lstrip().startswith("#"):
        i += 1
    # Skip blanks after the heading.
    while i < len(lines) and not lines[i].strip():
        i += 1
    # Collect the first paragraph (until the next blank line).
    para: list[str] = []
    while i < len(lines) and lines[i].strip():
        para.append(lines[i].strip())
        i += 1
    if not para:
        return name
    text = " ".join(para)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > _DESCRIPTION_CAP:
        # Cut on a word boundary if convenient.
        cut = text[: _DESCRIPTION_CAP].rsplit(" ", 1)[0]
        text = (cut or text[: _DESCRIPTION_CAP]).rstrip(",.;:") + "…"
    return text


# ---- Coercion helpers -----------------------------------------------


def _coerce_tools(name: str, raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, list) and all(isinstance(t, str) for t in raw):
        return tuple(raw)
    logger.warning("role %r tools must be list[str]; ignoring", name)
    return None


def _coerce_meta_tools(name: str, raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    logger.warning(
        "role %r meta_tools must be bool; defaulting to True", name
    )
    return True


def _coerce_model(name: str, raw: Any) -> str:
    """Resolve a frontmatter model string to its canonical form, or ""
    if absent. Empty means "inherit the caller's model"."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        logger.warning("role %r model must be string; ignoring", name)
        return ""
    return llms.resolve_model(raw)


# ---- File-tier loading ----------------------------------------------


def _load_role_file(md_path: Path) -> Role | None:
    """Parse one `.md` file into a Role. Returns None on read errors."""
    try:
        text = md_path.read_text()
    except OSError as e:
        logger.warning("role file %s unreadable: %s", md_path, e)
        return None

    fm, body = _parse_frontmatter(text)
    raw_name = md_path.stem
    name = _normalize_name(raw_name)
    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        description = _derive_description(name, body)
    return Role(
        name=name,
        model=_coerce_model(name, fm.get("model")),
        description=description,
        system_prompt=body.strip(),
        tools=_coerce_tools(name, fm.get("tools")),
        meta_tools=_coerce_meta_tools(name, fm.get("meta_tools")),
        source=md_path.resolve(),
    )


def _scan_dir(root: Path) -> dict[str, Role]:
    """Walk one tier root for `.md` role files, applying name-collision
    rules within the tier (lexicographic-first wins on
    case/dash/underscore collision; warn)."""
    if not root.exists():
        return {}
    found: dict[str, Role] = {}
    for md in sorted(root.glob("*.md")):
        role = _load_role_file(md)
        if role is None:
            continue
        if role.name in found:
            logger.warning(
                "role name %r collides on disk in %s "
                "(already loaded from %s; ignoring %s)",
                role.name,
                root,
                found[role.name].source,
                md,
            )
            continue
        found[role.name] = role
    return found


def _bundled_root() -> Path | None:
    try:
        return Path(str(resources.files(PACKAGE_ROLES_PKG)))
    except (ModuleNotFoundError, FileNotFoundError):
        return None


# ---- Legacy [models.<name>] backward-compat -------------------------


def _coerce_legacy_body(name: str, body: str, body_path: str) -> str:
    if body and body_path:
        logger.warning(
            "role %r has both system_prompt and system_prompt_path; "
            "using system_prompt",
            name,
        )
        return body
    if body:
        return body
    if not body_path:
        return ""
    p = Path(body_path)
    if not p.is_absolute():
        p = paths.config_dir() / body_path
    try:
        return p.read_text()
    except OSError as e:
        logger.warning(
            "role %r system_prompt_path %s unreadable: %s",
            name, body_path, e,
        )
        return ""


def _coerce_legacy_role(name: str, entry: Any) -> Role | None:
    if not isinstance(entry, dict):
        logger.warning("role %r is not a table; ignoring", name)
        return None
    model = entry.get("model")
    description = entry.get("description")
    if not isinstance(model, str) or not model:
        logger.warning("role %r missing or invalid model; ignoring", name)
        return None
    if not isinstance(description, str) or not description:
        logger.warning("role %r missing or invalid description; ignoring", name)
        return None
    body = entry.get("system_prompt", "") or ""
    body_path = entry.get("system_prompt_path", "") or ""
    if not isinstance(body, str):
        body = ""
    if not isinstance(body_path, str):
        body_path = ""
    return Role(
        name=_normalize_name(name),
        model=llms.resolve_model(model),
        description=description,
        system_prompt=_coerce_legacy_body(name, body, body_path),
        tools=_coerce_tools(name, entry.get("tools")),
        meta_tools=_coerce_meta_tools(name, entry.get("meta_tools")),
        source=None,
    )


def _legacy_roles() -> dict[str, Role]:
    """Load `[models.<name>]` entries from config.toml. Emits a
    one-time deprecation warning on first call per process."""
    cfg = config_mod.load()
    raw = cfg.get("models", {})
    if not isinstance(raw, dict) or not raw:
        return {}
    roles: dict[str, Role] = {}
    for name, entry in raw.items():
        role = _coerce_legacy_role(name, entry)
        if role is not None:
            roles[role.name] = role
    if roles:
        _emit_deprecation_warning(sorted(roles))
    return roles


_DEPRECATION_WARNED = False


def _emit_deprecation_warning(names: list[str]) -> None:
    global _DEPRECATION_WARNED
    if _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED = True
    logger.warning(
        "[models.<name>] role tables in config.toml are deprecated; "
        "found %d role(s): %s. Run `pyagent-roles migrate` to convert "
        "them to markdown files under <config-dir>/roles/.",
        len(names),
        ", ".join(names),
    )


def _reset_deprecation_warning() -> None:
    """Test helper: reset the one-time warning gate."""
    global _DEPRECATION_WARNED
    _DEPRECATION_WARNED = False


# ---- Public API -----------------------------------------------------


def load() -> dict[str, Role]:
    """Return the dict of all defined roles, indexed by canonical name.

    Tier precedence (later wins): bundled < legacy [models.<name>] <
    user-tier files < project-tier files. Re-reads on every call so
    edited files take effect on the next render without a restart.
    Invalid entries warn and skip — agent runs are never blocked by a
    typo in a role.
    """
    roles: dict[str, Role] = {}

    bundled_root = _bundled_root()
    if bundled_root is not None:
        roles.update(_scan_dir(bundled_root))

    # Legacy TOML form: lower than file-based tiers, higher than
    # bundled (so a user can shadow a bundled role with a TOML entry
    # if they really want, though we don't recommend it).
    roles.update(_legacy_roles())

    roles.update(_scan_dir(paths.config_dir() / "roles"))
    roles.update(_scan_dir(LOCAL_ROLES_DIR))

    return roles


def resolve(spec: str) -> tuple[str, Role | None]:
    """Resolve a role name OR raw provider/model string.

    - Empty string returns ("", None) — caller decides (typically
      inherit parent's model).
    - If `spec` matches a defined role (case/dash/underscore-
      insensitive), returns (role.model, role). When the role's model
      is empty, returns ("", role) so the spawn site falls back to
      the parent's model.
    - Otherwise treats `spec` as a provider/model string and
      resolves via `llms.resolve_model`. The returned Role is None.

    Roles are looked up first, so a user cannot name a role
    "anthropic" or another provider name — the role lookup wins.
    """
    if not spec:
        return "", None
    normalized = _normalize_name(spec)
    role = load().get(normalized)
    if role is not None:
        return role.model, role
    return llms.resolve_model(spec), None


def catalog() -> str:
    """Render the role catalog injected into the system prompt.

    Re-reads on each call so newly-authored role files appear on the
    next render. Returns an empty string when no roles are defined.
    """
    roles = load()
    if not roles:
        return ""
    lines = [
        "## Available subagent models",
        "",
        "Each entry is a role you can pass as the `model` argument to "
        "`spawn_subagent` (alongside raw provider/model strings). Role "
        "definitions live as markdown files under "
        "`pyagent/roles/`, `<config-dir>/roles/`, or `.pyagent/roles/`, "
        "and pin the model, default persona, and tool restrictions for "
        "that role.",
        "",
    ]
    for role in sorted(roles.values(), key=lambda r: r.name):
        model_label = role.model or "(inherits parent)"
        lines.append(f"- **{role.name}** ({model_label}): {role.description}")
    return "\n".join(lines)
