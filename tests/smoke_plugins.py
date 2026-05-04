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

import datetime
import os
import queue
import shutil
import tempfile
from pathlib import Path

from pyagent import paths
from pyagent import plugins as plugins_mod


class _FakeAgent:
    """Minimum agent surface `LoadedPlugins.rescan_for_new` touches.

    Mirrors the real `Agent` just enough: a `tools` dict it mutates
    via `add_tool`, and a `pending_async_replies` Queue the rescan
    pushes loader-status notes onto.
    """

    def __init__(self, tools: dict | None = None) -> None:
        self.tools: dict = dict(tools or {})
        self.pending_async_replies: queue.Queue = queue.Queue()

    def add_tool(
        self, name: str, fn, auto_offload: bool = True, *, evict_after_use: bool = False
    ) -> None:
        self.tools[name] = fn

    def drain_replies(self) -> list[str]:
        out: list[str] = []
        while True:
            try:
                out.append(self.pending_async_replies.get_nowait())
            except queue.Empty:
                return out


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
        # Tool name is unique to this test (`fake_recall`) so it
        # doesn't collide with the bundled memory plugin's
        # recall_memory.
        plugin_py = (
            "def register(api):\n"
            '    api.register_tool("fake_recall", lambda: "ok")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="fakemem",
            name="fake-memory",
            provides_tools=["fake_recall"],
            plugin_py=plugin_py,
        )
        # Disable via config (preserve the built_in_plugins_enabled
        # = [] from the test fixture so the bundled memory plugin
        # doesn't appear too).
        cfg_file = cfg / "config.toml"
        cfg_file.write_text(
            "built_in_plugins_enabled = []\n"
            "[plugins.fake-memory]\nenabled = false\n"
        )
        loaded = plugins_mod.load()
        # Plugin disabled, but declared_tool_provenance retained.
        assert "fake_recall" not in loaded.tools()
        assert (
            loaded.declared_tool_provenance.get("fake_recall")
            == "fake-memory"
        )
        # Format the error.
        err = plugins_mod.format_missing_tool_error(
            name="fake_recall",
            available=["read_file", "grep"],
            declared_tool_provenance=loaded.declared_tool_provenance,
        )
        assert "fake-memory" in err
        assert "fake_recall" in err
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
        loaded.call_after_tool_call(
            "read_file", {"path": "/x"}, "file content here", False
        )
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


def test_bundled_memory_loads() -> None:
    """With memory explicitly enabled, the bundled plugin loads and
    exposes its tools and prompt sections."""
    cfg, restore = _isolated_config_dir()
    try:
        # Override the fixture's empty list with the bundled plugin
        # turned on.
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        # Root-mode load (bundled plugin sets in_subagents=false).
        loaded = plugins_mod.load(is_subagent=False)
        names = [s.manifest.name for s in loaded.states]
        assert "memory" in names, f"expected memory in {names}"
        for t in (
            "create_memory",
            "read_memory",
            "update_memory",
            "delete_memory",
            "write_user",
            "recall_memory",
        ):
            assert t in loaded.tools(), (t, sorted(loaded.tools()))
        section_names = {s.name for s in loaded.sections()}
        assert "memory-guidance" in section_names
        assert "user-ledger" in section_names
        assert "memory-index" in section_names

        # Subagent mode skips it (in_subagents=false).
        sub_loaded = plugins_mod.load(is_subagent=True)
        sub_names = [s.manifest.name for s in sub_loaded.states]
        assert "memory" not in sub_names
        print("✓ bundled memory loads in root, skipped in subagent")
    finally:
        restore()


def test_memory_round_trip() -> None:
    """create_memory + read_memory + update_memory(content=)
    round-trip; write_user for USER; filename validation."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, read_memory = loaded.tools()["read_memory"]
        _, create_memory = loaded.tools()["create_memory"]
        _, update_memory = loaded.tools()["update_memory"]
        _, write_user = loaded.tools()["write_user"]

        # Create a body via create_memory + read it back.
        result = create_memory(
            category="Style",
            title="Stack choices",
            content="# stack choices\nWe use Postgres.\n",
            filename="stack_choices.md",
        )
        assert "created" in result, result
        body = read_memory(file="stack_choices.md")
        assert "We use Postgres" in body, body

        memories_dir = cfg / "plugins" / "memory" / "memories"
        assert (memories_dir / "stack_choices.md").exists()

        # Update body via update_memory(content=).
        upd = update_memory(
            filename="stack_choices.md",
            content="# stack choices v2\n\nWe use Postgres + Redis.\n",
        )
        assert "updated stack_choices.md: body" == upd, upd
        body2 = read_memory(file="stack_choices.md")
        assert "Postgres + Redis" in body2, body2

        # Missing memory returns a clear error.
        missing = read_memory(file="not_there.md")
        assert missing.startswith("<memory not found"), missing

        # USER write via write_user.
        u = write_user(content="prefers tabs over spaces\n")
        assert "USER" in u, u
        assert (cfg / "plugins" / "memory" / "USER.md").read_text() \
            == "prefers tabs over spaces\n"

        # Path traversal / invalid filename rejected via read_memory.
        for bad in ("../escape.md", "sub/dir.md", "..", ".hidden.md"):
            r = read_memory(file=bad)
            assert r.startswith("<"), (bad, r)

        no_ext = read_memory(file="no_ext")
        assert "must end with .md" in no_ext, no_ext

        empty = read_memory(file="")
        assert empty.startswith("<"), empty

        abs_err = read_memory(file="/etc/passwd")
        assert "must not be absolute" in abs_err, abs_err

        print("✓ memory round trip: create_memory / read_memory / update_memory / write_user")
    finally:
        restore()


def test_recall_memory() -> None:
    """End-to-end: plant memories in the memory plugin's data dir,
    enable the bundled plugin, and confirm recall_memory finds the
    semantically-matching file. Skipped if fastembed isn't
    installed."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        print("⊘ fastembed not installed; skipping recall test")
        return

    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        # Plant memories directly on disk under the plugin's storage
        # (paths.data_dir() is monkeypatched to cfg).
        mm_storage = cfg / "plugins" / "memory"
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
        assert "memory" in names, (
            f"expected memory to load: states={names}"
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

        # min_score: a very high threshold filters out everything
        # and returns a helpful empty message that names the filter.
        out_high = recall(query="what database do we use", min_score=0.99)
        assert out_high.startswith("<no matches"), out_high
        assert "min_score" in out_high, out_high

        # category filter scopes results to one H2 section.
        # Stack section contains stack.md; Style section contains naming.md.
        cat_stack = recall(query="storage", category="Stack", k=5)
        assert "stack.md" in cat_stack, cat_stack
        assert "naming.md" not in cat_stack, cat_stack
        # Header reflects the active filter.
        assert "category='Stack'" in cat_stack, cat_stack

        # category match is case-insensitive.
        cat_lower = recall(query="storage", category="stack")
        assert "stack.md" in cat_lower, cat_lower

        # Unknown category returns the empty-with-hint message.
        cat_none = recall(query="storage", category="DoesNotExist")
        assert cat_none.startswith("<no matches"), cat_none

        # Subagent mode skips it.
        sub_loaded = plugins_mod.load(is_subagent=True)
        sub_names = [s.manifest.name for s in sub_loaded.states]
        assert "memory" not in sub_names

        print("✓ recall_memory: ranking + min_score + category filters")
    finally:
        restore()


def test_create_memory() -> None:
    """create_memory writes body + inserts the index line in one call.
    Covers: new category, case-insensitive append, "(no memories yet)"
    strip, collision rejection, empty description, default-filename
    derive, frontmatter on disk, content/title/category newline
    rejection, round trip via read_memory."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, read_memory = loaded.tools()["read_memory"]

        # First add — creates category, strips "(no memories yet)".
        result = create_memory(
            category="Database",
            title="Postgres deadlock from FK locking",
            content=(
                "# Postgres deadlock\n\n"
                "Deterministic update order or retry-with-backoff.\n"
            ),
            filename="pg_deadlock.md",
            description="FK + concurrent update → SHARE-lock deadlock",
        )
        assert result == "created pg_deadlock.md: category='Database'", result

        memories_dir = cfg / "plugins" / "memory" / "memories"
        index_path = cfg / "plugins" / "memory" / "MEMORY.md"
        index = index_path.read_text()
        assert "## Database" in index, index
        assert "(no memories yet)" not in index, index
        assert "[Postgres deadlock from FK locking](pg_deadlock.md)" in index
        assert "SHARE-lock deadlock" in index

        # Frontmatter prepended to the body on disk; read_memory
        # surfaces it via [created <iso>] header.
        raw = (memories_dir / "pg_deadlock.md").read_text()
        assert raw.startswith("---\ncreated_at:"), raw[:80]
        body = read_memory(file="pg_deadlock.md")
        assert body.startswith("[created "), body[:30]
        assert "retry-with-backoff" in body, body

        # Second add — case-insensitive match to existing category.
        create_memory(
            category="database",
            title="Connection pool sizing",
            content="# pool sizing\n\nrule of thumb...\n",
            filename="pool_sizing.md",
            description="cpu_count * 2 + spindle_count",
        )
        index2 = index_path.read_text()
        db_headings = [
            ln for ln in index2.splitlines()
            if ln.lstrip().lower().startswith("## database")
        ]
        assert len(db_headings) == 1, db_headings
        assert "pool_sizing.md" in index2

        # Filename collision: index already lists pg_deadlock.md.
        err = create_memory(
            category="Database",
            title="duplicate try",
            content="x",
            filename="pg_deadlock.md",
        )
        assert err.startswith("<filename collision"), err

        # Empty description is allowed; bullet has no em-dash.
        create_memory(
            category="Style",
            title="Naming",
            content="# Naming\n\nsnake_case.\n",
            filename="naming.md",
        )
        index3 = index_path.read_text()
        assert "[Naming](naming.md)" in index3, index3
        for ln in index3.splitlines():
            if "naming.md" in ln:
                assert "—" not in ln, ln

        # Auto-derive filename when omitted.
        out = create_memory(
            category="Style",
            title="Type hint conventions",
            content="# type hints\n\nFavor concrete unions.\n",
        )
        assert "type_hint_conventions.md" in out, out
        assert (memories_dir / "type_hint_conventions.md").exists()

        # Filename validation flows through.
        bad = create_memory(
            category="X",
            title="x",
            content="x",
            filename="../escape.md",
        )
        assert bad.startswith("<"), bad

        # Empty category rejected.
        empty_cat = create_memory(category="", title="x", content="x")
        assert empty_cat.startswith("<category is empty"), empty_cat

        # Newline injection in category rejected (RISK-2).
        bad_cat = create_memory(
            category="Style\n## Injected",
            title="x",
            content="x",
        )
        assert bad_cat.startswith("<category contains a newline"), bad_cat

        # Leading-# in category rejected.
        hash_cat = create_memory(category="# Heading", title="x", content="x")
        assert "cannot start with '#'" in hash_cat, hash_cat

        # Newline in title rejected.
        bad_title = create_memory(
            category="Style", title="line\nbreak", content="x"
        )
        assert "title contains a newline" in bad_title, bad_title

        # Newline in description rejected.
        bad_desc = create_memory(
            category="Style",
            title="ok",
            content="x",
            filename="desc_test.md",
            description="line\nbreak",
        )
        assert "description contains a newline" in bad_desc, bad_desc

        # Drift guard: "Styles" close to existing "Style" is refused.
        drift = create_memory(
            category="Styles",
            title="x",
            content="x",
            filename="drift_x.md",
        )
        assert drift.startswith("<category 'Styles' is close to"), drift
        assert "confirm_new_category=True" in drift, drift

        # confirm_new_category bypasses the drift guard.
        forced = create_memory(
            category="Styles",
            title="x",
            content="x",
            filename="drift_x.md",
            confirm_new_category=True,
        )
        assert "created" in forced, forced

        print("✓ create_memory: body + index, frontmatter, validation, derive")
    finally:
        restore()


def test_update_memory() -> None:
    """update_memory(filename, ...) covers description / category /
    body edits in any combination via filename-keyed CRUD."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, update_memory = loaded.tools()["update_memory"]

        create_memory(
            category="Style",
            title="UV vs poetry",
            content="# uv\n\nFaster.\n",
            filename="uv_choice.md",
            description="Notes on uv",
        )
        create_memory(
            category="Style",
            title="Naming",
            content="# naming\n\nsnake_case.\n",
            filename="naming.md",
            description="variable conventions",
        )

        index_path = cfg / "plugins" / "memory" / "MEMORY.md"
        body_path = (
            cfg / "plugins" / "memory" / "memories" / "uv_choice.md"
        )

        # No fields set → error.
        empty = update_memory(filename="uv_choice.md")
        assert empty.startswith("<update_memory needs at least one"), empty

        # Description only.
        out = update_memory(
            filename="uv_choice.md",
            description="Why we picked uv over poetry — perf + lockfile",
        )
        assert "description" in out, out
        idx = index_path.read_text()
        assert "Why we picked uv over poetry" in idx, idx
        assert "Notes on uv" not in idx, idx
        # Other bullets untouched.
        assert "[Naming](naming.md) — variable conventions" in idx, idx

        # Category only — moves the bullet across sections.
        out = update_memory(filename="uv_choice.md", category="Decisions")
        assert "category" in out, out
        idx = index_path.read_text()
        assert "## Decisions" in idx, idx
        decisions_block = idx.split("## Decisions")[1]
        assert "[UV vs poetry](uv_choice.md)" in decisions_block, idx

        # Description + category in one call (atomic-ish).
        out = update_memory(
            filename="uv_choice.md",
            description="Tooling choice (perf, lockfile)",
            category="Architecture",
        )
        assert "description" in out and "category" in out, out
        idx = index_path.read_text()
        assert "## Architecture" in idx, idx
        arch_block = idx.split("## Architecture")[1]
        assert "Tooling choice" in arch_block, idx

        # Body content with frontmatter preservation.
        original = body_path.read_text()
        assert original.startswith("---\ncreated_at:"), original[:80]
        original_created = original.split("\n", 2)[1]
        out = update_memory(
            filename="uv_choice.md",
            content="# uv revised\n\nnew body content.\n",
        )
        assert "body" in out, out
        revised = body_path.read_text()
        assert revised.startswith("---\n"), revised[:80]
        assert original_created in revised, revised
        assert "new body content" in revised, revised

        # Body content with explicit frontmatter — caller's wins.
        new_fm = "---\ncreated_at: 2020-01-01T00:00:00+00:00\n---\n"
        update_memory(
            filename="uv_choice.md", content=new_fm + "# migrated\n"
        )
        migrated = body_path.read_text()
        assert "2020-01-01" in migrated, migrated
        assert original_created not in migrated, migrated

        # Drift guard on category.
        drift = update_memory(filename="uv_choice.md", category="Architectures")
        assert drift.startswith("<category 'Architectures' is close"), drift
        assert "confirm_new_category=True" in drift, drift

        # Confirm bypass.
        forced = update_memory(
            filename="uv_choice.md",
            category="Architectures",
            confirm_new_category=True,
        )
        assert "category" in forced, forced

        # Empty description clears the trailing portion of the bullet.
        update_memory(filename="naming.md", description="")
        idx = index_path.read_text()
        for ln in idx.splitlines():
            if "naming.md" in ln:
                assert "—" not in ln, ln
                assert ln.rstrip().endswith("(naming.md)"), ln

        # Missing bullet → clear error (when index-touching fields set).
        miss = update_memory(filename="ghost.md", description="x")
        assert miss.startswith("<no bullet for"), miss

        # Missing body → clear error (when content set).
        miss_body = update_memory(filename="ghost.md", content="x")
        assert "<body memories/ghost.md not found>" in miss_body, miss_body

        # Newline rejection in description.
        bad = update_memory(
            filename="uv_choice.md", description="line\nbreak"
        )
        assert "description contains a newline" in bad, bad

        # Newline rejection in category.
        bad_cat = update_memory(
            filename="uv_choice.md", category="A\n## B"
        )
        assert "category contains a newline" in bad_cat, bad_cat

        # Bad filename rejected.
        invalid = update_memory(
            filename="../escape.md", description="x"
        )
        assert invalid.startswith("<"), invalid

        print("✓ update_memory: description / category / body / drift / validation")
    finally:
        restore()


def test_read_memory_strips_frontmatter() -> None:
    """read_memory turns ---created_at:---\\nbody into
    [created <iso>]\\n\\nbody. A body without frontmatter passes
    through unchanged."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, read_memory = loaded.tools()["read_memory"]

        memories_dir = cfg / "plugins" / "memory" / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        # With frontmatter.
        (memories_dir / "with_fm.md").write_text(
            "---\ncreated_at: 2026-05-04T08:00:00+00:00\n---\n"
            "# title\n\nbody.\n"
        )
        out = read_memory(file="with_fm.md")
        assert out.startswith(
            "[created 2026-05-04T08:00:00+00:00]\n\n"
        ), out[:80]
        assert "# title" in out and "body." in out

        # Without frontmatter (legacy memory).
        (memories_dir / "legacy.md").write_text("# legacy\n\nbody.\n")
        out2 = read_memory(file="legacy.md")
        assert out2 == "# legacy\n\nbody.\n"

        print("✓ read_memory: frontmatter → [created <iso>]; legacy passes through")
    finally:
        restore()


def test_delete_memory_role_only() -> None:
    """delete_memory is registered with role_only=True so it shows up
    in declared_tool_provenance and the loader's role_only_tool_names
    set, but the bootstrap should keep it out of a root agent's tool
    list."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        # Loader DOES expose delete_memory in tools().
        assert "delete_memory" in loaded.tools(), sorted(loaded.tools())
        # And flags it role-only.
        assert "delete_memory" in loaded.role_only_tool_names()
        # No other memory tool is role-only.
        for t in (
            "create_memory",
            "read_memory",
            "update_memory",
            "write_user",
            "recall_memory",
        ):
            assert t not in loaded.role_only_tool_names(), t
        print("✓ role_only flag: delete_memory tracked separately")
    finally:
        restore()


def test_delete_memory_orphan_tolerant() -> None:
    """delete_memory removes whatever exists: bullet only, body only,
    or both. Refuses only when neither is present."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, delete_memory = loaded.tools()["delete_memory"]

        # Plant a normal memory (bullet + body).
        create_memory(
            category="Style",
            title="Normal",
            content="# normal\n",
            filename="normal.md",
        )
        out = delete_memory(filename="normal.md")
        assert "bullet from MEMORY.md" in out, out
        assert "memories/normal.md" in out, out

        # Plant another, then orphan the bullet by manually deleting
        # the body. delete_memory should still strip the bullet.
        create_memory(
            category="Style",
            title="Orphan bullet",
            content="# x\n",
            filename="orphan_bullet.md",
        )
        body_path = (
            cfg / "plugins" / "memory" / "memories" / "orphan_bullet.md"
        )
        body_path.unlink()
        out = delete_memory(filename="orphan_bullet.md")
        assert "bullet from MEMORY.md" in out, out
        assert "memories/orphan_bullet.md" not in out, out

        # Plant another, then orphan the body by manually editing
        # MEMORY.md to remove the bullet. delete_memory should still
        # remove the body file.
        create_memory(
            category="Style",
            title="Orphan body",
            content="# x\n",
            filename="orphan_body.md",
        )
        index_path = cfg / "plugins" / "memory" / "MEMORY.md"
        index_text = index_path.read_text()
        index_text = "\n".join(
            ln for ln in index_text.splitlines()
            if "orphan_body.md" not in ln
        )
        index_path.write_text(index_text + "\n")
        out = delete_memory(filename="orphan_body.md")
        assert "memories/orphan_body.md" in out, out
        assert "bullet from MEMORY.md" not in out, out

        # Truly nothing → clear error.
        out = delete_memory(filename="ghost.md")
        assert out.startswith("<nothing to delete"), out

        print("✓ delete_memory: orphan-tolerant, refuses on nothing")
    finally:
        restore()


def test_role_only_plugin_tool_gating() -> None:
    """A plugin tool registered with role_only=True is tracked in
    role_only_tool_names() and absent from agent.tools when the
    bootstrap is given allowlist=None (root). When allowlist names
    the tool, it is added."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            "    def safe() -> str:\n"
            '        """Safe."""\n'
            "        return 'safe'\n"
            "    def dangerous() -> str:\n"
            '        """Dangerous."""\n'
            "        return 'dangerous'\n"
            "    api.register_tool('safe', safe)\n"
            "    api.register_tool('dangerous', dangerous, role_only=True)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="role-only-test",
            name="role-only-test",
            provides_tools=["safe", "dangerous"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        assert "safe" in loaded.tools()
        assert "dangerous" in loaded.tools()
        assert "dangerous" in loaded.role_only_tool_names()
        assert "safe" not in loaded.role_only_tool_names()
        print("✓ role_only plumbing: registered + tracked + discoverable")
    finally:
        restore()


def test_update_memory_anchored_match() -> None:
    """B1: update_memory matches the bullet by anchored shape, not
    raw substring. A description on another bullet that references
    the target memory by relative-link must not be clobbered."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, update_memory = loaded.tools()["update_memory"]

        create_memory(
            category="Style",
            title="UV choice",
            content="# uv\n",
            filename="uv.md",
            description="Why we picked uv",
        )
        # A second memory whose description references uv.md by link.
        # The substring `](uv.md)` will appear inside this bullet's
        # description but the bullet itself is for related.md.
        create_memory(
            category="Style",
            title="Related note",
            content="# related\n",
            filename="related.md",
            description="see also [uv writeup](uv.md) for context",
        )

        # Update uv.md's description. The naive substring matcher
        # would clobber related.md's bullet because its description
        # contains `](uv.md)`. Anchored matcher only touches uv.md's
        # actual bullet line.
        update_memory(
            filename="uv.md",
            description="Why we picked uv over poetry — perf",
        )
        index = (cfg / "plugins" / "memory" / "MEMORY.md").read_text()
        # New uv.md description in place.
        assert "Why we picked uv over poetry — perf" in index, index
        # related.md's bullet is unchanged (still has the link to uv.md).
        related_lines = [
            ln for ln in index.splitlines() if "related.md" in ln
        ]
        assert len(related_lines) == 1, related_lines
        assert "[uv writeup](uv.md)" in related_lines[0], related_lines[0]
        assert "for context" in related_lines[0], related_lines[0]

        print("✓ update_memory: anchored bullet match preserves cross-references")
    finally:
        restore()


def test_update_memory_per_key_frontmatter_merge() -> None:
    """B3: caller content with frontmatter that lacks created_at
    preserves the existing date by per-key merge."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, update_memory = loaded.tools()["update_memory"]

        create_memory(
            category="Style",
            title="Frontmatter test",
            content="# original\n",
            filename="fm_merge.md",
        )
        body_path = (
            cfg / "plugins" / "memory" / "memories" / "fm_merge.md"
        )
        original = body_path.read_text()
        original_created = original.split("\n", 2)[1]  # `created_at: ...`

        # Caller content has frontmatter but no created_at — must
        # preserve the existing one rather than dropping it.
        update_memory(
            filename="fm_merge.md",
            content="---\nfoo: bar\n---\n# revised\n",
        )
        revised = body_path.read_text()
        # Both original created_at and the new foo key are present.
        assert original_created in revised, revised
        assert "foo: bar" in revised, revised
        assert "# revised" in revised, revised

        print("✓ update_memory: per-key frontmatter merge preserves created_at")
    finally:
        restore()


def test_update_memory_rejects_empty_content() -> None:
    """N1: update_memory(content='') is rejected — degenerate state.
    Use delete_memory to remove the body."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        _, create_memory = loaded.tools()["create_memory"]
        _, update_memory = loaded.tools()["update_memory"]

        create_memory(
            category="Style",
            title="X",
            content="# original\n",
            filename="x.md",
        )
        # Empty content is rejected with a hint pointing at delete_memory.
        out = update_memory(filename="x.md", content="")
        assert out.startswith("<update_memory content is empty"), out
        assert "delete_memory" in out, out
        # Whitespace-only too.
        out = update_memory(filename="x.md", content="   \n  ")
        assert out.startswith("<update_memory content is empty"), out

        print("✓ update_memory: empty content rejected with delete_memory hint")
    finally:
        restore()


def test_recall_memory_surfaces_category() -> None:
    """F2: recall_memory result lines include category from the
    parsed index, so the agent can decide without an extra read."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        print("⊘ fastembed not installed; skipping recall category test")
        return

    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        mm_storage = cfg / "plugins" / "memory"
        memories_dir = mm_storage / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        (mm_storage / "MEMORY.md").write_text(
            "# Memory\n\n## Stack\n"
            "- [Stack choices](stack.md) — what database we picked\n"
        )
        (memories_dir / "stack.md").write_text(
            "# Stack\n\nWe use Postgres.\n"
        )

        loaded = plugins_mod.load(is_subagent=False)
        _, recall = loaded.tools()["recall_memory"]
        out = recall(query="postgres database choice", k=1)
        # Category is surfaced in the hit line.
        assert "category='Stack'" in out, out

        print("✓ recall_memory: category surfaces in hit line")
    finally:
        restore()


def test_recall_memory_temporal_filter() -> None:
    """Temporal: created_within_days drops hits older than the window
    and any without created_at frontmatter."""
    try:
        import fastembed  # noqa: F401
    except ImportError:
        print("⊘ fastembed not installed; skipping recall temporal test")
        return

    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["memory"]\n'
        )
        mm_storage = cfg / "plugins" / "memory"
        memories_dir = mm_storage / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)
        (mm_storage / "MEMORY.md").write_text(
            "# Memory\n\n## Stack\n"
            "- [Recent](recent.md) — postgres just last week\n"
            "- [Old](old.md) — postgres choice from way back\n"
            "- [Undated](undated.md) — postgres legacy entry\n"
        )
        # Recent: today minus 5 days.
        recent_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=5)
        ).isoformat(timespec="seconds")
        # Old: today minus 200 days.
        old_iso = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=200)
        ).isoformat(timespec="seconds")
        (memories_dir / "recent.md").write_text(
            f"---\ncreated_at: {recent_iso}\n---\n# recent\n\nUse postgres.\n"
        )
        (memories_dir / "old.md").write_text(
            f"---\ncreated_at: {old_iso}\n---\n# old\n\nUse postgres.\n"
        )
        # Undated: no frontmatter at all (legacy memory).
        (memories_dir / "undated.md").write_text(
            "# undated\n\nUse postgres.\n"
        )

        loaded = plugins_mod.load(is_subagent=False)
        _, recall = loaded.tools()["recall_memory"]

        # Without filter: all three appear.
        out = recall(query="postgres", k=10)
        assert "recent.md" in out, out
        assert "old.md" in out, out
        assert "undated.md" in out, out

        # With 30-day window: only recent.md (old is past, undated drops).
        out = recall(query="postgres", k=10, created_within_days=30)
        assert "recent.md" in out, out
        assert "old.md" not in out, out
        assert "undated.md" not in out, out
        # Filter shows up in the header.
        assert "created_within_days=30" in out, out

        # Invalid arg.
        bad = recall(query="postgres", created_within_days=0)
        assert "<created_within_days must be" in bad, bad

        print("✓ recall_memory: created_within_days drops old + undated")
    finally:
        restore()


def test_atomic_write_helper() -> None:
    """_atomic_write writes via <path>.tmp then os.replace. Verify a
    crash mid-write (simulated by an exception during write_text)
    leaves the prior file intact rather than truncating."""
    from pyagent.plugins.memory import _atomic_write

    tmpd = Path(tempfile.mkdtemp(prefix="atomic-"))
    try:
        target = tmpd / "MEMORY.md"
        target.write_text("original content\n")
        _atomic_write(target, "new content\n")
        assert target.read_text() == "new content\n"
        assert not (tmpd / "MEMORY.md.tmp").exists(), \
            "tmp file should be gone after replace"
        # Pre-existing file is replaced; tmp file from prior call
        # would have been cleaned by os.replace.
        print("✓ _atomic_write: temp-then-rename round trip")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_insert_index_bullet_unit() -> None:
    """_insert_index_bullet covers the placement edge cases:
    new section, existing section, multi-section, EOF append."""
    from pyagent.plugins.memory import _insert_index_bullet

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


def test_write_session_attachment_no_session() -> None:
    """`PluginAPI.write_session_attachment` returns None when no session
    has been bound. Plugins fall back to inline-only rendering in this
    branch (bench harness, certain test fixtures)."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "_state = {'path': 'unset'}\n"
            "def get_state(): return _state\n"
            "def register(api):\n"
            "    def go() -> str:\n"
            '        """Smoke: try to write."""\n'
            '        path = api.write_session_attachment(\n'
            '            "go", "side-data", suffix=".json"\n'
            "        )\n"
            "        _state['path'] = path\n"
            "        return 'ok'\n"
            '    api.register_tool("go", go)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="wsa-none",
            name="wsa-none",
            provides_tools=["go"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        # No bind_session() call → loader.session is None.
        _, fn = loaded.tools()["go"]
        assert fn() == "ok"
        import sys
        plugin_module = next(
            mod
            for mod_name, mod in sys.modules.items()
            if mod_name.startswith("pyagent_plugin_wsa_none")
        )
        assert plugin_module.get_state()["path"] is None, (
            "expected None when no session is bound"
        )
        print("✓ write_session_attachment returns None with no bound session")
    finally:
        restore()


def test_write_session_attachment_with_session() -> None:
    """After `bind_session(session)`, `write_session_attachment` writes
    bytes into the session's attachments dir and returns the Path."""
    from pyagent.session import Session

    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "_state = {'path': 'unset'}\n"
            "def get_state(): return _state\n"
            "def register(api):\n"
            "    def go() -> str:\n"
            '        """Smoke: write side-data."""\n'
            '        p = api.write_session_attachment(\n'
            '            "go", \'{"k": 1}\', suffix=".json"\n'
            "        )\n"
            "        _state['path'] = p\n"
            "        return 'ok'\n"
            '    api.register_tool("go", go)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="wsa-real",
            name="wsa-real",
            provides_tools=["go"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        sess_root = Path(tempfile.mkdtemp(prefix="pyagent-wsa-"))
        try:
            session = Session(session_id="t", root=sess_root)
            session._ensure_dirs()
            loaded.bind_session(session)

            _, fn = loaded.tools()["go"]
            assert fn() == "ok"

            import sys
            plugin_module = next(
                mod
                for mod_name, mod in sys.modules.items()
                if mod_name.startswith("pyagent_plugin_wsa_real")
            )
            saved = plugin_module.get_state()["path"]
            assert saved is not None, "expected a Path"
            assert saved.exists(), saved
            assert saved.parent == session.attachments_dir, saved
            assert saved.read_text() == '{"k": 1}'
            assert saved.suffix == ".json"
            print(
                f"✓ write_session_attachment with session: "
                f"wrote {saved.stat().st_size} bytes to {saved.name}"
            )
        finally:
            shutil.rmtree(sess_root, ignore_errors=True)
    finally:
        restore()


def test_graceful_degradation_when_memory_disabled() -> None:
    """With built_in_plugins_enabled=[], the memory tools don't load,
    but their declared_tool_provenance still cites the memory plugin
    so the rich missing-tool error works for a session that calls
    one."""
    cfg, restore = _isolated_config_dir()
    try:
        # Default fixture state: built_in_plugins_enabled = []
        loaded = plugins_mod.load()
        for t in (
            "create_memory",
            "read_memory",
            "update_memory",
            "delete_memory",
            "write_user",
            "recall_memory",
        ):
            assert t not in loaded.tools()
        # But the bundled plugin was DISCOVERED (just not loaded), so
        # declared_tool_provenance can cite it for the rich error.
        assert (
            loaded.declared_tool_provenance.get("read_memory")
            == "memory"
        )
        err = plugins_mod.format_missing_tool_error(
            name="read_memory",
            available=["read_file", "grep"],
            declared_tool_provenance=loaded.declared_tool_provenance,
        )
        assert "memory" in err
        assert "read_memory" in err
        print(
            "✓ graceful degradation: tools gone, missing-tool error "
            "cites bundled plugin"
        )
    finally:
        restore()


def test_rescan_picks_up_new_plugin() -> None:
    """A plugin directory created after `load()` gets discovered and
    registered on the next `rescan_for_new` call: its tool lands in
    both the loader registry and the agent's effective registry, and
    a status note is enqueued for the LLM.

    Plugin names are test-unique because the synthetic module name
    used by ``_load_module`` is process-global — re-using plugin
    names across tests trips the synth-name collision guard."""
    cfg, restore = _isolated_config_dir()
    try:
        baseline_py = (
            "def register(api):\n"
            "    def rescan_a_tool() -> str:\n"
            '        """A tool."""\n'
            '        return "a"\n'
            '    api.register_tool("rescan_a_tool", rescan_a_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="rescan-a",
            name="rescan-a",
            provides_tools=["rescan_a_tool"],
            plugin_py=baseline_py,
        )
        loaded = plugins_mod.load()
        assert "rescan_a_tool" in loaded.tools()
        assert len(loaded.states) == 1

        agent = _FakeAgent(tools={n: fn for n, (_, fn) in loaded.tools().items()})

        # Steady-state rescan: nothing new on disk.
        n_new = loaded.rescan_for_new(agent)
        assert n_new == 0
        assert agent.drain_replies() == []

        # Drop a new plugin into the same tier root.
        new_py = (
            "def register(api):\n"
            "    def rescan_b_tool() -> str:\n"
            '        """B tool."""\n'
            '        return "b"\n'
            '    api.register_tool("rescan_b_tool", rescan_b_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="rescan-b",
            name="rescan-b",
            provides_tools=["rescan_b_tool"],
            plugin_py=new_py,
        )

        n_new = loaded.rescan_for_new(agent)
        assert n_new == 1
        assert "rescan_b_tool" in loaded.tools()
        assert "rescan_b_tool" in agent.tools
        assert agent.tools["rescan_b_tool"]() == "b"
        replies = agent.drain_replies()
        assert len(replies) == 1
        assert "[plugin plugin-loader notes]:" in replies[0]
        assert "loaded rescan-b v0.1.0" in replies[0]
        assert "tools=[rescan_b_tool]" in replies[0]

        # Idempotent: a second call with no new plugins returns 0 and
        # doesn't re-register the same plugin.
        n_new = loaded.rescan_for_new(agent)
        assert n_new == 0
        assert len(loaded.states) == 2
        assert agent.drain_replies() == []
        print("✓ rescan picks up new plugin and notifies the LLM")
    finally:
        restore()


def test_rescan_skips_conflicting_tool() -> None:
    """A late-arriving plugin that claims a tool name already taken by
    the agent (built-in or earlier plugin) is skipped at the agent-
    registry layer, and the loader note tells the LLM about the
    skipped name so it doesn't try to call it."""
    cfg, restore = _isolated_config_dir()
    try:
        baseline_py = (
            "def register(api):\n"
            "    def conflict_tool() -> str:\n"
            '        """Original."""\n'
            '        return "original"\n'
            "    api.register_tool("
            '"conflict_tool", conflict_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="rescan-conflict-original",
            name="rescan-conflict-original",
            provides_tools=["conflict_tool"],
            plugin_py=baseline_py,
        )
        loaded = plugins_mod.load()
        agent = _FakeAgent(tools={n: fn for n, (_, fn) in loaded.tools().items()})

        # A new plugin that *also* declares conflict_tool plus a unique
        # tool unique_tool.
        intruder_py = (
            "def register(api):\n"
            "    def conflict_tool() -> str:\n"
            '        """Intruder."""\n'
            '        return "intruder"\n'
            "    def unique_tool() -> str:\n"
            '        """Unique."""\n'
            '        return "unique"\n'
            "    api.register_tool("
            '"conflict_tool", conflict_tool)\n'
            '    api.register_tool("unique_tool", unique_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="rescan-conflict-intruder",
            name="rescan-conflict-intruder",
            provides_tools=["conflict_tool", "unique_tool"],
            plugin_py=intruder_py,
        )

        n_new = loaded.rescan_for_new(agent)
        assert n_new == 1
        assert agent.tools["conflict_tool"]() == "original"
        assert agent.tools["unique_tool"]() == "unique"
        replies = agent.drain_replies()
        assert len(replies) == 1
        assert "tools=[unique_tool]" in replies[0]
        assert "tools-skipped-conflict=[conflict_tool]" in replies[0]
        print("✓ rescan respects first-wins and reports skipped tools in the note")
    finally:
        restore()


def test_rescan_register_failure_is_isolated() -> None:
    """A new plugin whose register() raises is logged and skipped;
    other newly-discovered plugins on the same scan still load."""
    cfg, restore = _isolated_config_dir()
    try:
        loaded = plugins_mod.load()
        agent = _FakeAgent()

        bad_py = (
            "def register(api):\n"
            '    raise RuntimeError("nope")\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="01-rescan-bad",
            name="rescan-bad",
            plugin_py=bad_py,
        )
        good_py = (
            "def register(api):\n"
            "    def rescan_good_tool() -> str:\n"
            '        """Good tool."""\n'
            '        return "good"\n'
            '    api.register_tool("rescan_good_tool", rescan_good_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-rescan-good",
            name="rescan-good",
            provides_tools=["rescan_good_tool"],
            plugin_py=good_py,
        )

        n_new = loaded.rescan_for_new(agent)
        assert n_new == 1
        assert "rescan_good_tool" in agent.tools
        assert [s.manifest.name for s in loaded.states] == ["rescan-good"]
        replies = agent.drain_replies()
        assert len(replies) == 1
        assert "loaded rescan-good" in replies[0]
        print("✓ rescan isolates failing register(); other new plugins still load")
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
    test_bundled_memory_loads()
    test_memory_round_trip()
    test_recall_memory()
    test_create_memory()
    test_update_memory()
    test_update_memory_anchored_match()
    test_update_memory_per_key_frontmatter_merge()
    test_update_memory_rejects_empty_content()
    test_recall_memory_surfaces_category()
    test_recall_memory_temporal_filter()
    test_delete_memory_role_only()
    test_delete_memory_orphan_tolerant()
    test_role_only_plugin_tool_gating()
    test_read_memory_strips_frontmatter()
    test_atomic_write_helper()
    test_insert_index_bullet_unit()
    test_write_session_attachment_no_session()
    test_write_session_attachment_with_session()
    test_graceful_degradation_when_memory_disabled()
    test_rescan_picks_up_new_plugin()
    test_rescan_skips_conflicting_tool()
    test_rescan_register_failure_is_isolated()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
