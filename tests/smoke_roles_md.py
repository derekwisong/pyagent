"""End-to-end smoke for file-based markdown roles.

Covers the migration from `[models.<name>]` config tables to standalone
`.md` files under `pyagent/roles_bundled/`, `<config-dir>/roles/`, and
`./.pyagent/roles/`:

  - Frontmatter parsing (full, none, partial, malformed)
  - Description auto-derivation from the body
  - Case-insensitive lookup, dash/underscore normalization
  - Tier precedence (project > user > bundled)
  - Per-tier disk-name collisions warn-and-keep-first
  - Legacy `[models.<name>]` still resolves AND emits a one-time
    deprecation warning
  - `pyagent-roles migrate` produces equivalent .md files
  - `pyagent-roles list` finds bundled + user + project roles
  - `catalog()` reflects the file-based roles

Run with:

    .venv/bin/python -m tests.smoke_roles_md
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from pyagent import paths, roles, roles_cli


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_frontmatter_full(tmp: Path) -> None:
    """A role with every frontmatter field populated."""
    _write(
        tmp / ".pyagent" / "roles" / "alpha.md",
        '+++\nmodel = "pyagent/echo"\n'
        'tools = ["read_file", "grep"]\n'
        "meta_tools = false\n"
        'description = "Alpha role for tests."\n'
        "+++\n\n"
        "# Role: Alpha\n\nAlpha persona body.\n",
    )
    loaded = roles.load()
    role = loaded["alpha"]
    assert role.model == "pyagent/echo", role.model
    assert role.tools == ("read_file", "grep"), role.tools
    assert role.meta_tools is False, role.meta_tools
    assert role.description == "Alpha role for tests.", role.description
    assert "Alpha persona body" in role.system_prompt
    assert role.source is not None and role.source.name == "alpha.md"
    print("✓ frontmatter (full): all fields applied")


def test_frontmatter_none(tmp: Path) -> None:
    """A role with no frontmatter at all — defaults + auto-description."""
    _write(
        tmp / ".pyagent" / "roles" / "bare.md",
        "# Role: Bare\n\n"
        "Bare-bones operator that does the thing without ceremony.\n",
    )
    loaded = roles.load()
    role = loaded["bare"]
    assert role.model == "", role.model
    assert role.tools is None, role.tools
    assert role.meta_tools is True, role.meta_tools
    assert "ceremony" in role.description, role.description
    print("✓ frontmatter (none): defaults applied + description derived")


def test_frontmatter_partial(tmp: Path) -> None:
    """Partial frontmatter — only `tools`, no model/description."""
    _write(
        tmp / ".pyagent" / "roles" / "partial.md",
        "+++\n"
        'tools = ["read_file"]\n'
        "+++\n\n"
        "# Role: Partial\n\nPartial role first paragraph.\n",
    )
    loaded = roles.load()
    role = loaded["partial"]
    assert role.tools == ("read_file",), role.tools
    assert role.model == "", role.model
    assert "first paragraph" in role.description
    print("✓ frontmatter (partial): selective override + auto description")


def test_frontmatter_malformed(tmp: Path) -> None:
    """Bad TOML in frontmatter falls back to no frontmatter."""
    _write(
        tmp / ".pyagent" / "roles" / "broken.md",
        "+++\n"
        'tools = "not a list (this is bad TOML for our schema)\n'
        "+++\n\n"
        "# Role: Broken\n\nBroken-but-still-loads role body.\n",
    )
    loaded = roles.load()
    role = loaded["broken"]
    # Body still readable; structured fields took defaults.
    assert role.tools is None, role.tools
    assert "still-loads" in role.system_prompt
    print("✓ frontmatter (malformed): role still loads with defaults")


def test_description_derivation_skips_heading(tmp: Path) -> None:
    """Description should skip the leading heading line."""
    _write(
        tmp / ".pyagent" / "roles" / "headed.md",
        "# Role: Headed\n\nThis is the actual paragraph used as description.\n",
    )
    loaded = roles.load()
    assert loaded["headed"].description.startswith("This is the actual"), loaded[
        "headed"
    ].description
    print("✓ description: leading heading skipped, first paragraph used")


def test_case_insensitive_lookup(tmp: Path) -> None:
    """File `RESEARCHER.md` callable as `researcher` and any case variant."""
    _write(
        tmp / ".pyagent" / "roles" / "BIG.md",
        "+++\n" 'model = "pyagent/echo"\n' "+++\n\n# Role: Big\n\nDoes big things.\n",
    )
    loaded = roles.load()
    assert "big" in loaded
    m, role = roles.resolve("BIG")
    assert role is not None and role.name == "big", role
    m2, role2 = roles.resolve("Big")
    assert role2 is not None and role2.name == "big", role2
    print("✓ lookup: case-insensitive (BIG / Big / big all resolve)")


def test_dash_underscore_normalization(tmp: Path) -> None:
    """`software-engineer.md` → role name `software_engineer`."""
    _write(
        tmp / ".pyagent" / "roles" / "field-tester.md",
        "# Role: Field tester\n\nTries things in the field.\n",
    )
    loaded = roles.load()
    assert "field_tester" in loaded, sorted(loaded)
    # Lookup with either dashes or underscores works.
    _, r1 = roles.resolve("field-tester")
    _, r2 = roles.resolve("field_tester")
    assert r1 is not None and r2 is not None
    assert r1.name == r2.name == "field_tester"
    print("✓ filename: dash/underscore both normalize to underscores")


def test_tier_precedence(tmp: Path, user_dir: Path) -> None:
    """Project > user > bundled on canonical-name collision.

    `RESEARCHER.md` ships bundled. We add a user-tier file and a
    project-tier file with the same canonical name and confirm the
    project tier wins.
    """
    # User-tier role.
    user_role = user_dir / "roles" / "researcher.md"
    _write(
        user_role,
        '+++\nmodel = "pyagent/echo"\n+++\n\nUSER-TIER researcher persona.\n',
    )
    loaded = roles.load()
    role = loaded["researcher"]
    assert "USER-TIER" in role.system_prompt, role.system_prompt
    print("✓ tier: user beats bundled")

    # Project-tier role wins over user.
    proj_role = tmp / ".pyagent" / "roles" / "RESEARCHER.md"
    _write(
        proj_role,
        '+++\nmodel = "pyagent/loremipsum"\n+++\n\nPROJECT-TIER researcher.\n',
    )
    loaded = roles.load()
    role = loaded["researcher"]
    assert "PROJECT-TIER" in role.system_prompt, role.system_prompt
    assert role.model == "pyagent/loremipsum", role.model
    print("✓ tier: project beats user beats bundled")


def test_legacy_models_table_still_resolves_and_warns(tmp: Path, caplog) -> None:
    """[models.<name>] in config.toml still loads + emits one-time warning."""
    (tmp / ".pyagent" / "config.toml").write_text(
        '[models.legacyrole]\n'
        'model = "pyagent/echo"\n'
        'description = "Legacy role still works."\n'
        "system_prompt = \"You're a legacy role.\"\n"
    )
    roles._reset_deprecation_warning()
    with caplog.at_level(logging.WARNING, logger="pyagent.roles"):
        loaded = roles.load()
    assert "legacyrole" in loaded
    role = loaded["legacyrole"]
    assert role.model == "pyagent/echo"
    assert any("deprecated" in r.getMessage() for r in caplog.records), caplog.records
    print("✓ legacy: [models.legacyrole] resolved + deprecation warning emitted")

    # Second load() call within the same process should NOT emit
    # another deprecation warning — gated by the one-time flag.
    caplog.records.clear()
    with caplog.at_level(logging.WARNING, logger="pyagent.roles"):
        roles.load()
    assert not any("deprecated" in r.getMessage() for r in caplog.records), (
        f"deprecation warning fired twice: {caplog.records}"
    )
    print("✓ legacy: deprecation warning does not spam (one-time)")


def test_file_based_role_shadows_legacy(tmp: Path) -> None:
    """If both an .md role and a [models.<name>] entry exist with
    the same canonical name, the file-based form wins (newer
    authoring tool)."""
    (tmp / ".pyagent" / "config.toml").write_text(
        '[models.shared]\n'
        'model = "pyagent/loremipsum"\n'
        'description = "Legacy version."\n'
    )
    _write(
        tmp / ".pyagent" / "roles" / "shared.md",
        '+++\nmodel = "pyagent/echo"\n'
        'description = "File version."\n+++\n\nFile-based shared role.\n',
    )
    roles._reset_deprecation_warning()
    loaded = roles.load()
    role = loaded["shared"]
    assert role.model == "pyagent/echo", role.model
    assert role.description == "File version.", role.description
    print("✓ collision: file-based role shadows legacy [models.shared]")


def test_per_tier_disk_collision(tmp: Path) -> None:
    """Two files in the same tier that normalize to the same name —
    warn and keep the lexicographically-first."""
    _write(
        tmp / ".pyagent" / "roles" / "DUPE.md",
        "# Role: Dupe upper\n\nFrom DUPE.md.\n",
    )
    _write(
        tmp / ".pyagent" / "roles" / "dupe.md",
        "# Role: Dupe lower\n\nFrom dupe.md.\n",
    )
    loaded = roles.load()
    role = loaded["dupe"]
    # Sorted glob -> "DUPE.md" comes before "dupe.md" lexicographically.
    assert role.source is not None and role.source.name == "DUPE.md", role.source
    print("✓ per-tier collision: lexicographically-first wins")


def test_catalog_reflects_file_based_roles(tmp: Path) -> None:
    _write(
        tmp / ".pyagent" / "roles" / "catalog_role.md",
        '+++\nmodel = "pyagent/echo"\n+++\n\nA cataloged role.\n',
    )
    cat = roles.catalog()
    assert "catalog_role" in cat
    assert "pyagent/echo" in cat
    print("✓ catalog: file-based roles render in the system-prompt block")


def test_cli_list_runs(tmp: Path, user_dir: Path) -> None:
    """`pyagent-roles list` finds bundled + user + project roles."""
    # User-tier
    _write(user_dir / "roles" / "user_only.md", "# Role: User\n\nUser role.\n")
    # Project-tier
    _write(tmp / ".pyagent" / "roles" / "proj_only.md", "# Role: Proj\n\nProject role.\n")
    runner = CliRunner()
    result = runner.invoke(roles_cli.main, ["list"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "researcher" in out, out  # bundled
    assert "user_only" in out, out
    assert "proj_only" in out, out
    print("✓ pyagent-roles list: shows bundled + user + project tiers")


def test_cli_show_and_path(tmp: Path) -> None:
    _write(
        tmp / ".pyagent" / "roles" / "shown.md",
        '+++\nmodel = "pyagent/echo"\n+++\n\n# Role: Shown\n\nShown body.\n',
    )
    runner = CliRunner()

    show = runner.invoke(roles_cli.main, ["show", "shown"])
    assert show.exit_code == 0, show.output
    assert "Shown body" in show.output, show.output
    assert "pyagent/echo" in show.output

    path = runner.invoke(roles_cli.main, ["path", "shown"])
    assert path.exit_code == 0, path.output
    assert path.output.strip().endswith("shown.md"), path.output
    print("✓ pyagent-roles show / path: emit role content + resolved path")


def test_cli_init_idempotent(tmp: Path, user_dir: Path) -> None:
    """`pyagent-roles init` seeds config-dir/roles, idempotently."""
    runner = CliRunner()
    first = runner.invoke(roles_cli.main, ["init"])
    assert first.exit_code == 0, first.output
    assert "wrote" in first.output, first.output

    seeded = sorted(p.name for p in (user_dir / "roles").glob("*.md"))
    assert "RESEARCHER.md" in seeded, seeded
    assert "SOFTWARE_ENGINEER.md" in seeded
    assert "REVIEWER.md" in seeded
    assert "SCRIBE.md" in seeded

    # Re-run: every file should report skipped.
    second = runner.invoke(roles_cli.main, ["init"])
    assert second.exit_code == 0, second.output
    assert "skipped" in second.output
    assert "wrote" not in second.output.lower().replace(
        "(none)", ""
    ).split("done:")[0]
    print("✓ pyagent-roles init: idempotent (skips existing files)")


def test_cli_migrate(tmp: Path, user_dir: Path) -> None:
    """`pyagent-roles migrate` synthesizes .md files from [models.<name>]."""
    (tmp / ".pyagent" / "config.toml").write_text(
        '[models.tomigrate]\n'
        'model = "pyagent/echo"\n'
        'description = "A role to migrate."\n'
        'system_prompt = "Migrated persona."\n'
        'tools = ["read_file"]\n'
        'meta_tools = false\n'
    )
    runner = CliRunner()
    result = runner.invoke(roles_cli.main, ["migrate"])
    assert result.exit_code == 0, result.output
    out_path = user_dir / "roles" / "TOMIGRATE.md"
    assert out_path.exists(), out_path
    text = out_path.read_text()
    assert "pyagent/echo" in text
    assert "Migrated persona" in text
    assert "tools = " in text
    assert "meta_tools = false" in text

    # Migrated role is now resolvable as a file-based role.
    roles._reset_deprecation_warning()
    loaded = roles.load()
    assert "tomigrate" in loaded
    role = loaded["tomigrate"]
    # File-based resolution should now win — source set.
    assert role.source is not None, role

    # When the legacy entry has a non-empty system_prompt, the migrated
    # frontmatter should NOT pin description — auto-derivation from the
    # body is enough, matching the bundled-roles convention. The
    # description still gets resolved correctly via auto-derive.
    assert "description = " not in text, (
        f"description should be omitted when body is present: {text!r}"
    )
    assert role.description, "auto-derived description should be non-empty"
    print("✓ pyagent-roles migrate: writes .md and roles.load() picks it up")


def test_cli_migrate_dashed_name(tmp: Path, user_dir: Path) -> None:
    """Legacy role names with dashes (`[models.deep-thought]`) migrate
    to canonical `DEEP_THOUGHT.md` — uppercase + underscores, matching
    the bundled-roles convention. Lookup still works either way via
    `_normalize_name`."""
    (tmp / ".pyagent" / "config.toml").write_text(
        '[models.deep-thought]\n'
        'model = "pyagent/echo"\n'
        'description = "Dashed name."\n'
        'system_prompt = "Body content here."\n'
    )
    runner = CliRunner()
    result = runner.invoke(roles_cli.main, ["migrate"])
    assert result.exit_code == 0, result.output

    canonical = user_dir / "roles" / "DEEP_THOUGHT.md"
    dashed = user_dir / "roles" / "DEEP-THOUGHT.md"
    assert canonical.exists(), f"expected {canonical}, got dir: " + str(
        list((user_dir / "roles").iterdir())
    )
    assert not dashed.exists(), (
        f"unexpected dashed filename {dashed} — should have been "
        f"normalized to underscores"
    )

    # Lookup still works via the original dashed name (normalized).
    roles._reset_deprecation_warning()
    loaded = roles.load()
    assert "deep_thought" in loaded, list(loaded.keys())
    print(
        "✓ pyagent-roles migrate: dashed names → underscored filenames"
    )


def test_cli_migrate_no_body_keeps_description(
    tmp: Path, user_dir: Path
) -> None:
    """When the legacy [models.<name>] entry has no `system_prompt`,
    the migrated file must keep `description` in the frontmatter — the
    auto-derive has nothing to pull from."""
    (tmp / ".pyagent" / "config.toml").write_text(
        '[models.bare]\n'
        'model = "pyagent/echo"\n'
        'description = "Pin me explicitly."\n'
    )
    runner = CliRunner()
    result = runner.invoke(roles_cli.main, ["migrate"])
    assert result.exit_code == 0, result.output
    out_path = user_dir / "roles" / "BARE.md"
    assert out_path.exists()
    text = out_path.read_text()
    assert 'description = "Pin me explicitly."' in text, text
    print("✓ pyagent-roles migrate: keeps description when body is empty")


# ---- Test harness ---------------------------------------------------


class _CapLog:
    """Tiny pytest-style caplog stand-in for the smoke harness."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self._handler = logging.Handler()
        self._handler.emit = self.records.append  # type: ignore[assignment]

    class _Ctx:
        def __init__(self, parent: "_CapLog", logger_name: str, level: int) -> None:
            self.parent = parent
            self.logger = logging.getLogger(logger_name)
            self.level = level
            self._old_level = self.logger.level

        def __enter__(self):
            self.logger.addHandler(self.parent._handler)
            self.logger.setLevel(self.level)
            return self.parent

        def __exit__(self, *exc):
            self.logger.removeHandler(self.parent._handler)
            self.logger.setLevel(self._old_level)
            return False

    def at_level(self, level: int, logger: str = "pyagent.roles") -> "_CapLog._Ctx":
        return _CapLog._Ctx(self, logger, level)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-roles-md-smoke-"))
    user_dir = Path(tempfile.mkdtemp(prefix="pyagent-roles-md-userdir-"))
    os.chdir(tmp)
    print(f"cwd: {tmp}")
    print(f"config-dir (mocked): {user_dir}")

    # Stub paths.config_dir() for the duration of the smoke run so the
    # user-tier root is the temp dir, not the real ~/.config/pyagent.
    real_config_dir = paths.config_dir
    paths.config_dir = lambda: user_dir  # type: ignore[assignment]
    try:
        caplog = _CapLog()

        test_frontmatter_full(tmp)
        test_frontmatter_none(tmp)
        test_frontmatter_partial(tmp)
        test_frontmatter_malformed(tmp)
        test_description_derivation_skips_heading(tmp)
        test_case_insensitive_lookup(tmp)
        test_dash_underscore_normalization(tmp)
        test_tier_precedence(tmp, user_dir)
        test_legacy_models_table_still_resolves_and_warns(tmp, caplog)
        test_file_based_role_shadows_legacy(tmp)
        test_per_tier_disk_collision(tmp)
        test_catalog_reflects_file_based_roles(tmp)
        test_cli_list_runs(tmp, user_dir)
        test_cli_show_and_path(tmp)
        test_cli_init_idempotent(tmp, user_dir)
        test_cli_migrate(tmp, user_dir)
        test_cli_migrate_dashed_name(tmp, user_dir)
        test_cli_migrate_no_body_keeps_description(tmp, user_dir)

        print("\nALL CHECKS PASSED")
    finally:
        paths.config_dir = real_config_dir  # type: ignore[assignment]


if __name__ == "__main__":
    main()
