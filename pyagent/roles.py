"""Named subagent roles loaded from config.

A role is `name -> (model, description, optional persona, optional
tool allowlist, meta-tool gate)`. Roles let an orchestrator address a
subagent by purpose ("planner", "validator") instead of by raw
provider/model string, and centralize cost/capability decisions in
config.

Schema (in config.toml):

    [models.planner]
    model = "anthropic/claude-opus-4-7"
    description = "Deep reasoning, multi-step planning."
    system_prompt = '''
    You are a planner. Break tasks into steps before recommending edits.
    '''
    # OR (mutually exclusive with system_prompt):
    # system_prompt_path = "prompts/planner.md"
    tools = ["read_file", "grep", "list_directory", "fetch_url"]
    meta_tools = false

Roles are merged across all config tiers (project wins over user wins
over defaults). Invalid entries are warned and skipped — agent runs
are never blocked by a typo in a role.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyagent import config as config_mod
from pyagent import llms, paths

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Role:
    """Resolved role definition.

    Attributes:
        name: Role identifier (the `<name>` in `[models.<name>]`).
        model: Resolved provider/model string (e.g. "anthropic/claude-opus-4-7").
        description: One-line summary the orchestrator uses to decide
            when to spawn this role. Renders into the role catalog.
        system_prompt: Default subagent persona body. May be empty.
            Layered onto SOUL/TOOLS/PRIMER, *before* the spawn-time
            task body.
        tools: If non-None, the explicit tool allowlist for this role.
            None means inherit the default tool set.
        meta_tools: Whether this role's subagent can itself spawn
            further subagents. Default True (matches root behavior).
    """

    name: str
    model: str
    description: str
    system_prompt: str
    tools: tuple[str, ...] | None
    meta_tools: bool


def _coerce_body(name: str, body: str, body_path: str) -> str:
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


def _coerce_tools(name: str, raw: Any) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if isinstance(raw, list) and all(isinstance(t, str) for t in raw):
        return tuple(raw)
    logger.warning("role %r tools must be list[str]; ignoring", name)
    return None


def _coerce_role(name: str, entry: Any) -> Role | None:
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
    meta_tools = entry.get("meta_tools", True)
    if not isinstance(meta_tools, bool):
        logger.warning(
            "role %r meta_tools must be bool; defaulting to True", name
        )
        meta_tools = True
    return Role(
        name=name,
        model=llms.resolve_model(model),
        description=description,
        system_prompt=_coerce_body(name, body, body_path),
        tools=_coerce_tools(name, entry.get("tools")),
        meta_tools=meta_tools,
    )


def load() -> dict[str, Role]:
    """Return the dict of all defined roles, indexed by name.

    Re-reads config on each call so an edited config takes effect on
    the next render without restarting the agent. Invalid entries
    are skipped with a warning.
    """
    cfg = config_mod.load()
    raw = cfg.get("models", {})
    if not isinstance(raw, dict):
        logger.warning("config.models must be a table; ignoring")
        return {}
    roles: dict[str, Role] = {}
    for name, entry in raw.items():
        role = _coerce_role(name, entry)
        if role is not None:
            roles[name] = role
    return roles


def resolve(spec: str) -> tuple[str, Role | None]:
    """Resolve a role name OR raw provider/model string.

    - Empty string returns ("", None) — caller decides (typically
      inherit parent's model).
    - If `spec` matches a defined role, returns (role.model, role).
    - Otherwise treats `spec` as a provider/model string and
      resolves via `llms.resolve_model`. The returned `Role` is None.

    Roles are looked up first, so a user cannot name a role "anthropic"
    or another provider name — the role lookup wins.
    """
    if not spec:
        return "", None
    role = load().get(spec)
    if role is not None:
        return role.model, role
    return llms.resolve_model(spec), None


def catalog() -> str:
    """Render the role catalog injected into the system prompt.

    Re-reads config on each call so newly-defined roles appear on the
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
        "definitions live in `config.toml` and pin the model, default "
        "persona, and tool restrictions for that role.",
        "",
    ]
    for role in sorted(roles.values(), key=lambda r: r.name):
        lines.append(f"- **{role.name}** ({role.model}): {role.description}")
    return "\n".join(lines)
