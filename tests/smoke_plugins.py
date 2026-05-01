"""Smoke tests for the plugin loader.

Covers:
  - Discover and load a drop-in plugin with manifest + plugin.py.
  - Tool registration via api.register_tool flows to agent.tools.
  - Prompt sections (volatile and non-volatile) are placed correctly.
  - Lifecycle hooks fire (on_session_start, after_assistant_response,
    before_tool_call, after_tool_call, on_session_end).
  - [provides] mismatch fails the plugin loud and the agent still runs.
  - Soft-fail tool-name conflict between two plugins.
  - Missing-tool rich error names the disabled-but-installed plugin.
  - Cache stability: bytes inside the stable segment don't change when
    the volatile renderer's content mutates.
  - in_subagents=false skips the plugin in subagent mode.
  - Helper modules (multi-file plugins) import via relative imports.

Run with:

    .venv/bin/python -m tests.smoke_plugins
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from pyagent import paths
from pyagent import plugins as plugins_mod


def _write_plugin(
    plugins_root: Path,
    dirname: str,
    *,
    name: str,
    provides_tools: list[str] | None = None,
    provides_sections: list[str] | None = None,
    in_subagents: bool = True,
    plugin_py: str = "",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Create a drop-in plugin directory with manifest + plugin.py."""
    pdir = plugins_root / dirname
    pdir.mkdir(parents=True, exist_ok=True)
    tools_line = (
        "tools = ["
        + ", ".join(f'"{t}"' for t in (provides_tools or []))
        + "]"
    )
    sections_line = (
        "prompt_sections = ["
        + ", ".join(f'"{s}"' for s in (provides_sections or []))
        + "]"
    )
    in_sub_line = "true" if in_subagents else "false"
    manifest = (
        f'name = "{name}"\n'
        f'version = "0.1.0"\n'
        f'description = "{name} plugin (smoke test)"\n'
        f'api_version = "1"\n\n'
        "[provides]\n"
        f"{tools_line}\n"
        f"{sections_line}\n\n"
        "[load]\n"
        f"in_subagents = {in_sub_line}\n"
    )
    (pdir / "manifest.toml").write_text(manifest)
    (pdir / "plugin.py").write_text(plugin_py)
    for fname, content in (extra_files or {}).items():
        path = pdir / fname
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return pdir


def _isolated_config_dir() -> tuple[Path, callable]:
    """Point pyagent.paths.config_dir() and paths.data_dir() at a temp
    dir for the test.

    Pre-seeds the temp config.toml with `built_in_plugins_enabled = []`
    so the bundled memory-markdown plugin doesn't appear in test
    fixtures by default. Tests that want bundled plugins enabled can
    overwrite the config file.

    Returns the dir and a restore function. (One temp dir backs both
    config and data — tests don't care about the split, and a shared
    fixture keeps cleanup trivial.)
    """
    tmp_cfg = Path(tempfile.mkdtemp(prefix="pyagent-plugin-cfg-"))
    (tmp_cfg / "config.toml").write_text(
        "built_in_plugins_enabled = []\n"
    )
    original_config = paths.config_dir
    original_data = paths.data_dir
    paths.config_dir = lambda: tmp_cfg  # type: ignore[assignment]
    paths.data_dir = lambda: tmp_cfg  # type: ignore[assignment]

    def restore() -> None:
        paths.config_dir = original_config  # type: ignore[assignment]
        paths.data_dir = original_data  # type: ignore[assignment]
        shutil.rmtree(tmp_cfg, ignore_errors=True)

    return tmp_cfg, restore


def test_basic_load_and_register() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            "    def hello(name: str) -> str:\n"
            '        """Say hi."""\n'
            '        return f"hi {name}"\n'
            '    api.register_tool("hello", hello)\n'
            "    def render(ctx):\n"
            '        return "## Hello plugin guidance"\n'
            '    api.register_prompt_section("hello-section", render, volatile=False)\n'
            "    state = {}\n"
            "    api.on_session_start(lambda s: state.setdefault('start', True))\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="hello",
            name="hello",
            provides_tools=["hello"],
            provides_sections=["hello-section"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 1
        assert loaded.states[0].manifest.name == "hello"
        assert "hello" in loaded.tools()
        plugin_name, fn = loaded.tools()["hello"]
        assert plugin_name == "hello"
        assert fn(name="world") == "hi world"
        assert len(loaded.sections()) == 1
        assert loaded.sections()[0].name == "hello-section"
        assert loaded.sections()[0].volatile is False
        print("✓ basic load + register_tool + register_prompt_section")
    finally:
        restore()


def test_provides_mismatch() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Plugin declares two tools but only registers one.
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("hello", lambda: "hi")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="bad",
            name="bad",
            provides_tools=["hello", "missing_tool"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 0, (
            "plugin with [provides] mismatch should be skipped"
        )
        print("✓ [provides] mismatch fails plugin loud")
    finally:
        restore()


def test_register_raises() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            '    raise RuntimeError("boom")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="boom",
            name="boom",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 0
        print("✓ register() raise → plugin skipped, no crash")
    finally:
        restore()


def test_soft_fail_tool_conflict() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Both plugins try to register `same`. First-loaded wins;
        # second's registration is dropped from the resolved tools.
        plugin_a = (
            "def register(api):\n"
            '    api.register_tool("same", lambda: "from-a")\n'
        )
        plugin_b = (
            "def register(api):\n"
            '    api.register_tool("same", lambda: "from-b")\n'
        )
        # Use directory prefixes to make load order deterministic.
        _write_plugin(
            cfg / "plugins",
            dirname="01-a",
            name="plugin-a",
            provides_tools=["same"],
            plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-b",
            name="plugin-b",
            provides_tools=["same"],
            plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 2
        plugin_name, fn = loaded.tools()["same"]
        assert plugin_name == "plugin-a"
        assert fn() == "from-a"
        # declared_tool_provenance should still know about both.
        assert loaded.declared_tool_provenance["same"] == "plugin-a"
        print("✓ soft-fail conflict: first directory wins")
    finally:
        restore()


def test_missing_tool_error() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Discover-but-disable a plugin so its tool name is in
        # declared_tool_provenance but not in the registered tools.
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("recall_memory", lambda: "ok")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="memvec",
            name="memory-vector",
            provides_tools=["recall_memory"],
            plugin_py=plugin_py,
        )
        # Disable via config (preserve the built_in_plugins_enabled
        # = [] from the test fixture so the bundled memory-markdown
        # doesn't appear too).
        cfg_file = cfg / "config.toml"
        cfg_file.write_text(
            "built_in_plugins_enabled = []\n"
            "[plugins.memory-vector]\nenabled = false\n"
        )
        loaded = plugins_mod.load()
        # Plugin disabled, but declared_tool_provenance retained.
        assert "recall_memory" not in loaded.tools()
        assert (
            loaded.declared_tool_provenance.get("recall_memory")
            == "memory-vector"
        )
        # Format the error.
        err = plugins_mod.format_missing_tool_error(
            name="recall_memory",
            available=["read_file", "grep"],
            declared_tool_provenance=loaded.declared_tool_provenance,
        )
        assert "memory-vector" in err
        assert "recall_memory" in err
        assert "read_file" in err
        print("✓ rich missing-tool error cites disabled plugin")
    finally:
        restore()


def test_in_subagents_false() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("root_only", lambda: "ok")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="root-only",
            name="root-only",
            provides_tools=["root_only"],
            in_subagents=False,
            plugin_py=plugin_py,
        )
        # Root mode loads it.
        root_loaded = plugins_mod.load(is_subagent=False)
        assert "root_only" in root_loaded.tools()
        # Subagent mode skips it.
        sub_loaded = plugins_mod.load(is_subagent=True)
        assert "root_only" not in sub_loaded.tools()
        assert len(sub_loaded.states) == 0
        print("✓ [load] in_subagents=false skips plugin in subagent mode")
    finally:
        restore()


def test_volatile_section_placement() -> None:
    """A non-volatile section lives in `stable`; a volatile one in
    `volatile`. Bytes inside `stable` don't change when the volatile
    renderer's output mutates."""
    from pyagent.prompts import SystemPromptBuilder

    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "_count = [0]\n"
            "def register(api):\n"
            "    def stable_render(ctx):\n"
            '        return "## Stable always-the-same"\n'
            "    def volatile_render(ctx):\n"
            "        _count[0] += 1\n"
            '        return f"## Volatile turn {_count[0]}"\n'
            '    api.register_prompt_section("stable-s", stable_render, volatile=False)\n'
            '    api.register_prompt_section("volatile-s", volatile_render, volatile=True)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="cache-test",
            name="cache-test",
            provides_sections=["stable-s", "volatile-s"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        # Set up SystemPromptBuilder
        tmp = Path(tempfile.mkdtemp(prefix="cache-test-soul-"))
        for nm in ("SOUL.md", "TOOLS.md", "PRIMER.md"):
            (tmp / nm).write_text(f"# {nm}\n")
        original_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            builder = SystemPromptBuilder(
                soul=tmp / "SOUL.md",
                tools=tmp / "TOOLS.md",
                primer=tmp / "PRIMER.md",
                plugin_loader=loaded,
            )
            stable1, volatile1 = builder.build_segments()
            stable2, volatile2 = builder.build_segments()
            assert "Stable always-the-same" in stable1
            assert "Volatile turn 1" in volatile1
            assert "Volatile turn 2" in volatile2
            assert stable1 == stable2, (
                "stable segment must not change when volatile renderer"
                " output mutates"
            )
            assert volatile1 != volatile2
            print("✓ volatile renderer mutation does not change stable bytes")
        finally:
            os.chdir(original_cwd)
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        restore()


def test_helper_module_import() -> None:
    """A plugin can import a helper module sitting alongside plugin.py."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from . import helper\n"
            "def register(api):\n"
            '    api.register_tool("hello_via_helper", helper.hello)\n'
        )
        helper_py = (
            "def hello() -> str:\n"
            '    """Greeting from helper."""\n'
            '    return "from helper"\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="multi-file",
            name="multi-file",
            provides_tools=["hello_via_helper"],
            plugin_py=plugin_py,
            extra_files={"helper.py": helper_py},
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 1
        _, fn = loaded.tools()["hello_via_helper"]
        assert fn() == "from helper"
        print("✓ multi-file plugin: helper module imports via 'from .'")
    finally:
        restore()


def test_lifecycle_hooks_fire() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "events = []\n"
            "def register(api):\n"
            "    api.on_session_start(lambda s: events.append('start'))\n"
            "    api.on_session_end(lambda s: events.append('end'))\n"
            "    api.after_assistant_response(lambda t: events.append(('ar', t)))\n"
            "    api.before_tool_call(lambda n, a: events.append(('btc', n, dict(a))))\n"
            "    api.after_tool_call(lambda n, a, r: events.append(('atc', n, r[:10])))\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="lifecycle",
            name="lifecycle",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        plugin_module = None
        # Find the loaded module for the events list.
        import sys
        for mod_name, mod in sys.modules.items():
            if mod_name.startswith("pyagent_plugin_lifecycle"):
                plugin_module = mod
                break
        assert plugin_module is not None
        events = plugin_module.events

        loaded.call_on_session_start(session=None)
        assert events == ["start"]

        loaded.call_after_assistant_response("hello there")
        loaded.call_before_tool_call("read_file", {"path": "/x"})
        loaded.call_after_tool_call("read_file", {"path": "/x"}, "file content here")
        loaded.call_on_session_end(session=None)

        assert events[0] == "start"
        assert events[1] == ("ar", "hello there")
        assert events[2] == ("btc", "read_file", {"path": "/x"})
        assert events[3] == ("atc", "read_file", "file conte")
        assert events[4] == "end"
        print("✓ lifecycle + observation hooks fire in order")
    finally:
        restore()


def test_hook_failure_isolation() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Two hooks in one plugin: first raises, second records.
        # Verifies per-hook isolation (try/except inside the inner
        # loop).
        plugin_py = (
            "_state = {'seen': None}\n"
            "def get_state(): return _state\n"
            "def register(api):\n"
            "    def boom(text):\n"
            "        raise RuntimeError('boom')\n"
            "    def record(text):\n"
            "        _state['seen'] = text\n"
            "    api.after_assistant_response(boom)\n"
            "    api.after_assistant_response(record)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="iso",
            name="iso",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        # Should not raise even though the first hook raises.
        loaded.call_after_assistant_response("hello")
        # Pull the plugin module's state to verify the second hook ran.
        import sys
        plugin_module = next(
            mod
            for mod_name, mod in sys.modules.items()
            if mod_name.startswith("pyagent_plugin_iso")
        )
        assert plugin_module.get_state()["seen"] == "hello", (
            "second hook must fire even when first one raised"
        )
        print("✓ hook raise is isolated; subsequent hooks still fire")
    finally:
        restore()


def test_directory_prefix_load_order() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # 02-second uses directory prefix to load AFTER 01-first even
        # though the manifest names sort the other way.
        first = (
            "def register(api):\n"
            '    api.register_tool("conflict", lambda: "first-wins")\n'
        )
        second = (
            "def register(api):\n"
            '    api.register_tool("conflict", lambda: "second-loses")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="01-second-named",  # loaded first by directory order
            name="zzz-late-by-name",
            provides_tools=["conflict"],
            plugin_py=first,
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-first-named",
            name="aaa-early-by-name",
            provides_tools=["conflict"],
            plugin_py=second,
        )
        loaded = plugins_mod.load()
        assert len(loaded.states) == 2
        plugin_name, fn = loaded.tools()["conflict"]
        # The plugin in `01-...` directory wins, regardless of manifest name.
        assert plugin_name == "zzz-late-by-name"
        assert fn() == "first-wins"
        print("✓ directory-name prefix controls load order")
    finally:
        restore()


def test_api_version_mismatch() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Manifest with wrong api_version — plugin must be skipped.
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("never", lambda: "ok")\n'
        )
        pdir = cfg / "plugins" / "future"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "manifest.toml").write_text(
            'name = "future"\n'
            'version = "0.1.0"\n'
            'description = "from the future"\n'
            'api_version = "999"\n\n'
            '[provides]\n'
            'tools = ["never"]\n'
        )
        (pdir / "plugin.py").write_text(plugin_py)
        loaded = plugins_mod.load()
        assert len(loaded.states) == 0
        print("✓ api_version mismatch → plugin skipped")
    finally:
        restore()


def test_message_wrapping() -> None:
    """make_prompt_context normalizes mixed conversation entries
    into Message objects so plugins can read .text uniformly."""
    conv = [
        {"role": "user", "content": "hi there"},
        {"role": "assistant", "text": "hello back", "tool_calls": []},
        {"role": "user", "tool_results": [{"id": "1", "name": "x", "content": "..."}]},
        {"role": "assistant", "text": "", "tool_calls": [{"id": "1", "name": "x", "args": {}}]},
        {"role": "user", "content": "follow up"},
    ]
    ctx = plugins_mod.make_prompt_context(conv)
    assert len(ctx.recent_messages) == 5
    assert ctx.recent_messages[0].role == "user"
    assert ctx.recent_messages[0].text == "hi there"
    assert ctx.recent_messages[1].role == "assistant"
    assert ctx.recent_messages[1].text == "hello back"
    # tool-result turn → user role, empty text
    assert ctx.recent_messages[2].role == "user"
    assert ctx.recent_messages[2].text == ""
    # assistant with only tool_calls → empty text
    assert ctx.recent_messages[3].role == "assistant"
    assert ctx.recent_messages[3].text == ""
    assert ctx.recent_messages[4].text == "follow up"
    print("✓ make_prompt_context normalizes heterogeneous turns into Message")


def test_immutable_returns() -> None:
    """LoadedPlugins.tools()/sections() return immutable views so a
    misbehaving consumer can't corrupt the resolved registry."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("hello", lambda: "hi")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="immut",
            name="immut",
            provides_tools=["hello"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        try:
            loaded.tools()["sneaky"] = ("evil", lambda: None)
            raise AssertionError("tools() should be immutable")
        except TypeError:
            pass
        # sections() returns a tuple; tuple has no .append
        assert isinstance(loaded.sections(), tuple)
        print("✓ tools() and sections() return immutable views")
    finally:
        restore()


def test_builtin_tool_takes_precedence_in_agent() -> None:
    """When a plugin tool name collides with a built-in registered on
    the agent first, the built-in keeps its slot. (This is enforced
    by agent_proc._bootstrap, not by the plugin loader itself, but
    we can verify the loader doesn't conflict.)"""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("read_file", lambda path: "fake")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="evil-readfile",
            name="evil-readfile",
            provides_tools=["read_file"],
            plugin_py=plugin_py,
        )
        # Plugin loads — loader doesn't know about built-ins.
        loaded = plugins_mod.load()
        assert "read_file" in loaded.tools()
        # The agent_proc._bootstrap path skips the plugin tool when a
        # built-in already has the name. We can't easily exercise that
        # here without spinning up a full agent process, so we just
        # verify the loader's contract.
        plugin_name, _ = loaded.tools()["read_file"]
        assert plugin_name == "evil-readfile"
        print("✓ loader does not police built-in collision (agent_proc does)")
    finally:
        restore()


def test_bundled_memory_markdown_loads() -> None:
    """With memory-markdown explicitly enabled, the bundled plugin
    loads and exposes its tools/sections."""
    cfg, restore = _isolated_config_dir()
    try:
        # Override the fixture's empty list with the bundled plugin
        # turned on.
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory-markdown"]\n'
        )
        # Root-mode load (bundled plugin sets in_subagents=false).
        loaded = plugins_mod.load(is_subagent=False)
        names = [s.manifest.name for s in loaded.states]
        assert "memory-markdown" in names, (
            f"expected memory-markdown in {names}"
        )
        assert "read_ledger" in loaded.tools()
        assert "write_ledger" in loaded.tools()
        section_names = {s.name for s in loaded.sections()}
        assert "memory-guidance" in section_names
        assert "user-ledger" in section_names
        assert "memory-index" in section_names

        # Subagent mode skips it (in_subagents=false).
        sub_loaded = plugins_mod.load(is_subagent=True)
        sub_names = [s.manifest.name for s in sub_loaded.states]
        assert "memory-markdown" not in sub_names
        print("✓ bundled memory-markdown loads in root, skipped in subagent")
    finally:
        restore()


def test_memory_per_file_round_trip() -> None:
    """write_ledger("MEMORY", content, file=...) creates
    memories/<file>; read_ledger("MEMORY", file=...) returns it.
    Validates filename rejection and USER+file refusal too."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory-markdown"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, read_ledger = loaded.tools()["read_ledger"]
        _, write_ledger = loaded.tools()["write_ledger"]

        # Round trip.
        result = write_ledger(
            name="MEMORY",
            content="# stack choices\nWe use Postgres.\n",
            file="stack_choices.md",
        )
        assert "Wrote" in result, result
        body = read_ledger(name="MEMORY", file="stack_choices.md")
        assert "We use Postgres" in body, body

        # The file landed under memories/.
        memories_dir = cfg / "plugins" / "memory-markdown" / "memories"
        assert (memories_dir / "stack_choices.md").exists()

        # Missing memory returns a clear error string.
        missing = read_ledger(name="MEMORY", file="not_there.md")
        assert missing.startswith("<memory not found"), missing

        # USER + file is rejected.
        err_user_read = read_ledger(name="USER", file="x.md")
        assert "single-file ledger" in err_user_read, err_user_read
        err_user_write = write_ledger(
            name="USER", content="x", file="x.md"
        )
        assert "single-file ledger" in err_user_write, err_user_write

        # Path traversal rejected.
        for bad in ("../escape.md", "sub/dir.md", "..", ".hidden.md"):
            r = read_ledger(name="MEMORY", file=bad)
            assert r.startswith("<"), (bad, r)

        # Non-.md rejected.
        no_ext = read_ledger(name="MEMORY", file="no_ext")
        assert "must end with .md" in no_ext, no_ext

        # Empty filename rejected.
        empty = read_ledger(name="MEMORY", file="")
        assert empty.startswith("<"), empty

        # Reading MEMORY without file returns the index (the seeded
        # template, since we haven't overwritten it).
        index = read_ledger(name="MEMORY")
        assert "Index" in index or "memory" in index.lower(), index

        print("✓ memory per-file round trip + validation")
    finally:
        restore()


def test_memory_vector_recall() -> None:
    """End-to-end: plant memories in memory-markdown's data dir,
    enable both bundled plugins, and confirm recall_memory finds
    the semantically-matching file. Skipped if fastembed isn't
    installed."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        print("⊘ fastembed not installed; skipping memory-vector test")
        return

    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = '
            '["memory-markdown", "memory-vector"]\n'
        )
        # Plant memories directly on disk under memory-markdown's
        # storage (paths.data_dir() is monkeypatched to cfg).
        mm_storage = cfg / "plugins" / "memory-markdown"
        memories_dir = mm_storage / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        (mm_storage / "MEMORY.md").write_text(
            "# Memory\n\n## Stack\n"
            "- [Stack choices](stack.md) — what database we picked\n"
            "## Style\n"
            "- [Naming](naming.md) — variable and class casing\n"
        )
        (memories_dir / "stack.md").write_text(
            "# Stack\n\nWe use Postgres for primary storage. "
            "Redis for caches.\n"
        )
        (memories_dir / "naming.md").write_text(
            "# Naming\n\nVariables: snake_case. Classes: PascalCase. "
            "Constants: UPPER_SNAKE.\n"
        )

        loaded = plugins_mod.load(is_subagent=False)
        names = [s.manifest.name for s in loaded.states]
        assert "memory-vector" in names, (
            f"expected memory-vector to load: states={names}"
        )
        assert "recall_memory" in loaded.tools()

        _, recall = loaded.tools()["recall_memory"]

        # A database-flavored query should rank stack.md above naming.md.
        result = recall(query="what database do we use", k=2)
        assert "stack.md" in result, result
        # The first hit (top of ranked output) should be stack.md.
        first_hit_line = next(
            ln for ln in result.splitlines() if "memories/" in ln
        )
        assert "stack.md" in first_hit_line, first_hit_line

        # An empty query returns a clear error.
        empty = recall(query="")
        assert empty.startswith("<"), empty

        # Subagent mode skips it.
        sub_loaded = plugins_mod.load(is_subagent=True)
        sub_names = [s.manifest.name for s in sub_loaded.states]
        assert "memory-vector" not in sub_names

        print("✓ memory-vector recall: semantic ranking works end-to-end")
    finally:
        restore()


def test_add_memory_tool() -> None:
    """add_memory writes body + inserts the index line in one call.
    Covers: new category creation, case-insensitive append to
    existing, "(no memories yet)" placeholder strip, filename
    collision rejection, empty hook, round trip with read_ledger."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory-markdown"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, add_memory = loaded.tools()["add_memory"]
        _, read_ledger = loaded.tools()["read_ledger"]

        # First add — creates the category, strips "(no memories yet)".
        result = add_memory(
            category="Database",
            title="Postgres deadlock from FK locking",
            filename="pg_deadlock.md",
            hook="FK + concurrent update → SHARE-lock deadlock; retry with backoff",
            content="# Postgres deadlock\n\nDeterministic update order or retry-with-backoff.\n",
        )
        assert "Wrote" in result and "Database" in result, result

        index = read_ledger(name="MEMORY")
        assert "## Database" in index, index
        assert "(no memories yet)" not in index, index
        assert "[Postgres deadlock from FK locking](pg_deadlock.md)" in index
        assert "SHARE-lock deadlock" in index

        body = read_ledger(name="MEMORY", file="pg_deadlock.md")
        assert "retry-with-backoff" in body, body

        # Second add — case-insensitive match to existing category;
        # bullet appends under the same heading.
        add_memory(
            category="database",  # lowercase
            title="Connection pool sizing",
            filename="pool_sizing.md",
            hook="rule of thumb: cpu_count * 2 + spindle_count",
            content="# pool sizing\n\nrule of thumb...\n",
        )
        index2 = read_ledger(name="MEMORY")
        # No duplicate "## Database" / "## database" headings.
        db_headings = [
            ln for ln in index2.splitlines()
            if ln.lstrip().lower().startswith("## database")
        ]
        assert len(db_headings) == 1, db_headings
        assert "pg_deadlock.md" in index2
        assert "pool_sizing.md" in index2

        # Filename collision: the on-disk body already exists.
        err = add_memory(
            category="Database",
            title="duplicate try",
            filename="pg_deadlock.md",
            hook="should fail",
            content="x",
        )
        assert err.startswith("<filename collision"), err
        assert "pick a more specific filename" in err, err

        # Empty hook is allowed; bullet just has no dash-and-text.
        add_memory(
            category="Style",
            title="Naming",
            filename="naming.md",
            hook="",
            content="# Naming\n\nsnake_case.\n",
        )
        index3 = read_ledger(name="MEMORY")
        assert "[Naming](naming.md)" in index3, index3
        # No "— " for empty hook.
        for ln in index3.splitlines():
            if "naming.md" in ln:
                assert "—" not in ln, ln

        # Filename validation flows through.
        bad = add_memory(
            category="X",
            title="x",
            filename="../escape.md",
            hook="",
            content="x",
        )
        assert bad.startswith("<"), bad

        # Empty category rejected.
        empty_cat = add_memory(
            category="",
            title="x",
            filename="x.md",
            hook="",
            content="x",
        )
        assert empty_cat.startswith("<"), empty_cat

        print("✓ add_memory: body + index in one call, no re-emit")
    finally:
        restore()


def test_insert_index_bullet_unit() -> None:
    """_insert_index_bullet covers the placement edge cases:
    new section, existing section, multi-section, EOF append."""
    from pyagent.plugins.memory_markdown import _insert_index_bullet

    # Empty + placeholder → strip, append new section.
    seed = "# Memory\n\n(no memories yet)\n"
    out = _insert_index_bullet(seed, "Database", "- [a](a.md) — hook")
    assert "(no memories yet)" not in out
    assert "## Database\n- [a](a.md) — hook" in out

    # Existing section, multiple sections, blank between → bullet
    # joins the right cluster.
    existing = (
        "# Memory\n\n## Database\n- [foo](foo.md) — bar\n\n"
        "## Style\n- [naming](naming.md)\n"
    )
    out = _insert_index_bullet(existing, "Database", "- [new](new.md)")
    db_block = out.split("## Style")[0]
    assert "- [new](new.md)" in db_block, out
    assert "[naming](naming.md)" in out

    # Case-insensitive heading match — no duplicate H2 inserted.
    out2 = _insert_index_bullet(existing, "DATABASE", "- [up](up.md)")
    assert "- [up](up.md)" in out2.split("## Style")[0]
    assert out2.lower().count("## database") == 1, out2

    # No matching heading → append at end.
    out3 = _insert_index_bullet(existing, "Gotchas", "- [g](g.md)")
    tail = out3.rstrip().splitlines()[-2:]
    assert tail == ["## Gotchas", "- [g](g.md)"], tail

    # Empty input → just heading + bullet.
    out4 = _insert_index_bullet("", "Cat", "- [a](a.md)")
    assert out4.startswith("## Cat\n- [a](a.md)"), out4

    print("✓ _insert_index_bullet: placement edge cases")


def test_graceful_degradation_when_memory_disabled() -> None:
    """With built_in_plugins_enabled=[], the agent has no ledger
    tools and no ledger prose, but its declared_tool_provenance
    still cites memory-markdown so the rich missing-tool error works
    on a session that previously called read_ledger."""
    cfg, restore = _isolated_config_dir()
    try:
        # Default fixture state: built_in_plugins_enabled = []
        loaded = plugins_mod.load()
        # No memory plugin loaded.
        assert "read_ledger" not in loaded.tools()
        assert "write_ledger" not in loaded.tools()
        # But the bundled plugin was DISCOVERED (just not loaded), so
        # declared_tool_provenance can cite it for the rich error.
        assert (
            loaded.declared_tool_provenance.get("read_ledger")
            == "memory-markdown"
        )
        err = plugins_mod.format_missing_tool_error(
            name="read_ledger",
            available=["read_file", "grep"],
            declared_tool_provenance=loaded.declared_tool_provenance,
        )
        assert "memory-markdown" in err
        assert "read_ledger" in err
        print(
            "✓ graceful degradation: tools gone, missing-tool error "
            "cites bundled plugin"
        )
    finally:
        restore()


def main() -> None:
    test_basic_load_and_register()
    test_provides_mismatch()
    test_register_raises()
    test_soft_fail_tool_conflict()
    test_missing_tool_error()
    test_in_subagents_false()
    test_volatile_section_placement()
    test_helper_module_import()
    test_lifecycle_hooks_fire()
    test_hook_failure_isolation()
    test_directory_prefix_load_order()
    test_api_version_mismatch()
    test_message_wrapping()
    test_immutable_returns()
    test_builtin_tool_takes_precedence_in_agent()
    test_bundled_memory_markdown_loads()
    test_memory_per_file_round_trip()
    test_memory_vector_recall()
    test_add_memory_tool()
    test_insert_index_bullet_unit()
    test_graceful_degradation_when_memory_disabled()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
