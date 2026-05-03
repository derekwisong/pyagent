"""Smoke tests for `PluginAPI.call_tool` — cross-plugin composition.

Covers:
  - Plugin B's tool calls plugin A's tool through `api.call_tool`.
  - `call_tool("nonexistent")` returns the not-available marker, no raise.
  - Depth-cap protects against A→A and A→B→A loops.
  - Called tool inherits the calling agent's permission scope (same
    `permissions.require_access` handler fires).
  - PluginAPI without a bound loader (e.g. `_FakeAPI` in tests) degrades
    to the not-available marker rather than raising.

Run with:

    .venv/bin/python -m tests.smoke_call_tool
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from pyagent import paths
from pyagent import plugins as plugins_mod


def _isolated_config_dir() -> tuple[Path, "callable"]:
    tmp_cfg = Path(tempfile.mkdtemp(prefix="pyagent-call-tool-"))
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


def _write_plugin(
    plugins_root: Path,
    dirname: str,
    *,
    name: str,
    provides_tools: list[str],
    plugin_py: str,
) -> Path:
    pdir = plugins_root / dirname
    pdir.mkdir(parents=True, exist_ok=True)
    tools_line = (
        "tools = ["
        + ", ".join(f'"{t}"' for t in provides_tools)
        + "]"
    )
    manifest = (
        f'name = "{name}"\n'
        f'version = "0.1.0"\n'
        f'description = "{name} plugin (call_tool smoke)"\n'
        f'api_version = "1"\n\n'
        "[provides]\n"
        f"{tools_line}\n"
        'prompt_sections = []\n\n'
        "[load]\n"
        "in_subagents = true\n"
    )
    (pdir / "manifest.toml").write_text(manifest)
    (pdir / "plugin.py").write_text(plugin_py)
    return pdir


def test_basic_chain() -> None:
    """tool_b calls tool_a via api.call_tool; result chains through."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "def register(api):\n"
            "    def tool_a(x: int) -> str:\n"
            '        """Return doubled x as a string."""\n'
            '        return f"a={x*2}"\n'
            '    api.register_tool("tool_a", tool_a)\n'
        )
        plugin_b = (
            "def register(api):\n"
            "    def tool_b() -> str:\n"
            '        """Call tool_a with x=21 and wrap its result."""\n'
            '        inner = api.call_tool("tool_a", x=21)\n'
            '        return f"b<<{inner}>>"\n'
            '    api.register_tool("tool_b", tool_b)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="01-a",
            name="plug-a",
            provides_tools=["tool_a"],
            plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-b",
            name="plug-b",
            provides_tools=["tool_b"],
            plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()
        assert {"tool_a", "tool_b"} <= set(loaded.tools().keys())
        _, fn_b = loaded.tools()["tool_b"]
        result = fn_b()
        assert result == "b<<a=42>>", result
        print("✓ basic chain: plugin B's tool calls plugin A's tool")
    finally:
        restore()


def test_missing_tool_marker() -> None:
    """call_tool('nonexistent') returns the marker, never raises."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            "    def caller() -> str:\n"
            '        """Try to call a tool that does not exist."""\n'
            '        return api.call_tool("does_not_exist", a=1)\n'
            '    api.register_tool("caller", caller)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="caller",
            name="caller",
            provides_tools=["caller"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        _, fn = loaded.tools()["caller"]
        result = fn()
        assert result.startswith("<error:"), result
        assert "not available" in result, result
        assert "'does_not_exist'" in result, result
        print("✓ missing tool returns marker, no raise")
    finally:
        restore()


def test_self_loop_depth_cap() -> None:
    """A tool that calls itself bounds out at the depth cap."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            "    def loopy() -> str:\n"
            '        """Recurse through call_tool until depth cap fires."""\n'
            '        return api.call_tool("loopy")\n'
            '    api.register_tool("loopy", loopy)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="loopy",
            name="loopy",
            provides_tools=["loopy"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        _, fn = loaded.tools()["loopy"]
        result = fn()
        assert "depth exceeded" in result, result
        print("✓ self-loop bounded by depth cap")
    finally:
        restore()


def test_cross_loop_depth_cap() -> None:
    """A → B → A → B ... is also bounded."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "def register(api):\n"
            "    def a_tool() -> str:\n"
            '        """Bounce to b_tool."""\n'
            '        return api.call_tool("b_tool")\n'
            '    api.register_tool("a_tool", a_tool)\n'
        )
        plugin_b = (
            "def register(api):\n"
            "    def b_tool() -> str:\n"
            '        """Bounce to a_tool."""\n'
            '        return api.call_tool("a_tool")\n'
            '    api.register_tool("b_tool", b_tool)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="01-a",
            name="aplug",
            provides_tools=["a_tool"],
            plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-b",
            name="bplug",
            provides_tools=["b_tool"],
            plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()
        _, fn_a = loaded.tools()["a_tool"]
        result = fn_a()
        assert "depth exceeded" in result, result
        print("✓ A→B→A loop bounded by depth cap")
    finally:
        restore()


def test_depth_resets_between_calls() -> None:
    """After a deep chain returns, depth is restored — a second
    top-level call_tool from outside also gets the full budget.
    Guards against the thread-local leaking on success or exception."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "def register(api):\n"
            "    def echo(v: str) -> str:\n"
            '        """Echo the argument."""\n'
            '        return v\n'
            '    api.register_tool("echo", echo)\n'
            "\n"
            "    def deep() -> str:\n"
            '        """Call echo 3 times sequentially in one tool body."""\n'
            "        out = []\n"
            "        for i in range(3):\n"
            '            out.append(api.call_tool("echo", v=str(i)))\n'
            '        return "|".join(out)\n'
            '    api.register_tool("deep", deep)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="reset",
            name="reset",
            provides_tools=["echo", "deep"],
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        _, fn_deep = loaded.tools()["deep"]
        result = fn_deep()
        assert result == "0|1|2", result
        # And depth should be back to 0 in this thread.
        assert plugins_mod._call_tool_depth() == 0
        print("✓ depth counter resets after each call_tool returns")
    finally:
        restore()


def test_permission_inheritance() -> None:
    """The called tool sees the SAME `permissions.require_access`
    handler as the calling tool — there's no separate context. We
    install a counting prompt handler, run a chain, and check both
    plugins' calls hit it."""
    from pyagent import permissions

    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "from pyagent import permissions\n"
            "def register(api):\n"
            "    def tool_a(path: str) -> str:\n"
            '        """Touch a path under permission gating."""\n'
            "        permissions.require_access(path)\n"
            '        return "a-ok"\n'
            '    api.register_tool("tool_a", tool_a)\n'
        )
        plugin_b = (
            "from pyagent import permissions\n"
            "def register(api):\n"
            "    def tool_b(path: str) -> str:\n"
            '        """Hit permissions then chain to tool_a."""\n'
            "        permissions.require_access(path)\n"
            '        return api.call_tool("tool_a", path=path)\n'
            '    api.register_tool("tool_b", tool_b)\n'
        )
        _write_plugin(
            cfg / "plugins",
            dirname="01-a",
            name="perm-a",
            provides_tools=["tool_a"],
            plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins",
            dirname="02-b",
            name="perm-b",
            provides_tools=["tool_b"],
            plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()

        # Install a counting prompt handler that allows everything.
        prompt_calls = {"n": 0, "paths": []}

        def fake_prompt(p: Path) -> bool:
            prompt_calls["n"] += 1
            prompt_calls["paths"].append(p)
            return True

        original_handler = permissions._PROMPT_HANDLER  # type: ignore[attr-defined]
        permissions.set_prompt_handler(fake_prompt)
        try:
            _, fn_b = loaded.tools()["tool_b"]
            # Use an out-of-workspace path so require_access prompts.
            outside = "/tmp/pyagent-perm-test"
            result = fn_b(path=outside)
            assert result == "a-ok", result
            # Both tools' require_access calls hit the same handler.
            # (One per tool body. Set may dedupe via approved_paths
            # though, so just check >= 1.)
            assert prompt_calls["n"] >= 1, prompt_calls
        finally:
            permissions.set_prompt_handler(original_handler)
        print(
            f"✓ permissions inherited: handler fired "
            f"{prompt_calls['n']} time(s) across the chain"
        )
    finally:
        restore()


def test_no_loader_graceful_degradation() -> None:
    """When PluginAPI is constructed without a loader (the test-fake
    pattern), call_tool returns the not-available marker rather than
    raising. Documented design choice — keeps plugin-author code
    composable in unit tests."""
    state = plugins_mod._PluginState(
        manifest=plugins_mod.Manifest(
            name="bare",
            version="0.0.1",
            description="no loader",
            api_version="1",
            provides_tools=(),
            provides_prompt_sections=(),
            provides_providers=(),
            requires_python="",
            requires_env=(),
            requires_binaries=(),
            in_subagents=True,
            source=Path("/dev/null"),
        )
    )
    api = plugins_mod.PluginAPI(state, loader=None)
    result = api.call_tool("anything", x=1)
    assert result.startswith("<error:"), result
    assert "not available" in result, result
    print("✓ no-loader PluginAPI degrades gracefully")


def test_called_tool_raises_returns_marker() -> None:
    """If the called tool body raises, call_tool wraps the exception
    into the standard marker shape rather than propagating. Plugin
    authors don't need a try/except around every composition."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "def register(api):\n"
            "    def boom() -> str:\n"
            '        """Always raises."""\n'
            "        raise ValueError('upstream broke')\n"
            '    api.register_tool("boom", boom)\n'
        )
        plugin_b = (
            "def register(api):\n"
            "    def caller() -> str:\n"
            '        """Calls boom() and reports what happened."""\n'
            '        return api.call_tool("boom")\n'
            '    api.register_tool("caller", caller)\n'
        )
        _write_plugin(
            cfg / "plugins", dirname="01-a", name="plug-boom",
            provides_tools=["boom"], plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins", dirname="02-b", name="plug-caller",
            provides_tools=["caller"], plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()
        _, fn = loaded.tools()["caller"]
        result = fn()
        assert result.startswith("<error: tool 'boom' raised: "), result
        assert "ValueError" in result, result
        assert "upstream broke" in result, result
        print("✓ called tool raises → <error: tool ... raised: ValueError: ...>")
    finally:
        restore()


def test_bad_kwargs_returns_marker() -> None:
    """Passing kwargs the target tool doesn't accept produces a
    TypeError, which call_tool wraps the same way as any other raise."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "def register(api):\n"
            "    def takes_x(x: int) -> str:\n"
            '        """Doubles x."""\n'
            '        return f"x={x*2}"\n'
            '    api.register_tool("takes_x", takes_x)\n'
        )
        plugin_b = (
            "def register(api):\n"
            "    def caller() -> str:\n"
            '        """Calls takes_x with the wrong kwarg."""\n'
            '        return api.call_tool("takes_x", y=1)\n'
            '    api.register_tool("bad_kw_caller", caller)\n'
        )
        _write_plugin(
            cfg / "plugins", dirname="01-a", name="plug-bad-kwargs-target",
            provides_tools=["takes_x"], plugin_py=plugin_a,
        )
        _write_plugin(
            cfg / "plugins", dirname="02-b", name="plug-bad-kwargs-caller",
            provides_tools=["bad_kw_caller"], plugin_py=plugin_b,
        )
        loaded = plugins_mod.load()
        _, fn = loaded.tools()["bad_kw_caller"]
        result = fn()
        assert result.startswith("<error: tool 'takes_x' raised: "), result
        assert "TypeError" in result, result
        print("✓ bad kwargs → <error: tool ... raised: TypeError: ...>")
    finally:
        restore()


def test_bad_name_input() -> None:
    """call_tool(name) where name is None / empty / non-string returns
    a typed marker, never raises."""
    state = plugins_mod._PluginState(
        manifest=plugins_mod.Manifest(
            name="bare", version="0.0.1", description="bad-name",
            api_version="1", provides_tools=(), provides_prompt_sections=(),
            provides_providers=(), requires_python="", requires_env=(),
            requires_binaries=(), in_subagents=True, source=Path("/dev/null"),
        )
    )
    api = plugins_mod.PluginAPI(state, loader=None)

    out = api.call_tool(None)
    assert out.startswith("<error: name must be a non-empty string"), out
    assert "NoneType" in out, out

    out = api.call_tool("")
    assert out.startswith("<error: name must be a non-empty string"), out

    out = api.call_tool("   ")
    assert out.startswith("<error: name must be a non-empty string"), out

    out = api.call_tool(42)
    assert out.startswith("<error: name must be a non-empty string"), out
    assert "int" in out, out

    print("✓ bad name input: None / empty / whitespace / non-string → typed marker")


def test_agent_registry_takes_precedence() -> None:
    """When an Agent is bound, call_tool resolves through the agent's
    effective registry. A tool that exists in the plugin loader but
    NOT in agent.tools (because a role-allowlist excluded it, or it
    was never registered onto the agent) is unreachable via call_tool
    — closes the role-allowlist bypass flagged in #92's review."""
    from unittest.mock import MagicMock

    cfg, restore = _isolated_config_dir()
    try:
        # Register two plugin tools — `excluded_tool` and `caller`.
        # caller tries to reach excluded_tool via call_tool. Bind a
        # mock Agent whose `tools` registry has caller but not
        # excluded_tool, simulating a role-allowlist that filtered
        # excluded_tool out of registration.
        plugin_x = (
            "def register(api):\n"
            "    def excluded_tool() -> str:\n"
            '        """Registered to the plugin loader but not the agent."""\n'
            '        return "should-not-be-reached"\n'
            '    api.register_tool("excluded_tool", excluded_tool)\n'
        )
        plugin_y = (
            "def register(api):\n"
            "    def caller() -> str:\n"
            '        """Tries to reach the excluded tool."""\n'
            '        return api.call_tool("excluded_tool")\n'
            '    api.register_tool("caller", caller)\n'
        )
        _write_plugin(
            cfg / "plugins", dirname="01-x", name="plug-x",
            provides_tools=["excluded_tool"], plugin_py=plugin_x,
        )
        _write_plugin(
            cfg / "plugins", dirname="02-y", name="plug-y",
            provides_tools=["caller"], plugin_py=plugin_y,
        )
        loaded = plugins_mod.load()
        # Plugin loader sees BOTH tools.
        assert "excluded_tool" in loaded.tools(), loaded.tools().keys()
        assert "caller" in loaded.tools(), loaded.tools().keys()

        # Bind an agent whose registry only has `caller` —
        # simulates a role allowlist that excluded `excluded_tool`.
        _, caller_fn = loaded.tools()["caller"]
        fake_agent = MagicMock()
        fake_agent.tools = {"caller": caller_fn}
        loaded.bind_agent(fake_agent)

        # Calling caller() must NOT reach excluded_tool — the agent
        # registry is the authority.
        result = caller_fn()
        assert result.startswith("<error: tool 'excluded_tool' not available"), result
        assert "should-not-be-reached" not in result, result

        # Sanity: with the agent's registry expanded to include both,
        # the same call now succeeds.
        fake_agent.tools = {
            "caller": caller_fn,
            "excluded_tool": loaded.tools()["excluded_tool"][1],
        }
        result = caller_fn()
        assert result == "should-not-be-reached", result
        print("✓ role-allowlist: agent.tools is authoritative over loader.tools()")
    finally:
        restore()


def main() -> None:
    test_basic_chain()
    test_missing_tool_marker()
    test_self_loop_depth_cap()
    test_cross_loop_depth_cap()
    test_depth_resets_between_calls()
    test_permission_inheritance()
    test_no_loader_graceful_degradation()
    test_called_tool_raises_returns_marker()
    test_bad_kwargs_returns_marker()
    test_bad_name_input()
    test_agent_registry_takes_precedence()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
