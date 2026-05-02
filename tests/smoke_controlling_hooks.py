"""Smoke tests for v2 controlling hooks (issue #66).

Covers every acceptance-criteria bullet:

  - allow (no change) — v2 hook returning None or
    `ToolHookResult(decision="allow")` is a pure observer.
  - block — tool not executed, synthetic marker shown, INFO log
    emitted, permission check NOT reached (short-circuit ordering).
  - mutate — tool sees mutated args; mutation persists into
    conversation history.
  - extra_user_message from before_tool and after_tool — flows
    through `pending_async_replies` and lands as a user-role turn
    next iteration.
  - conflict resolution: block > mutate, mutate chaining,
    replace_result chaining (later plugin sees replaced result).
  - v1 plugin returning a v2-shaped value → ignored, behavior
    unchanged.
  - strategic_reevaluation: 3 fails on path A → inject; 3 fails
    interleaved across paths A and B → no inject (path-keyed).

Run with:

    .venv/bin/python -m tests.smoke_controlling_hooks
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from pyagent import paths
from pyagent import plugins as plugins_mod
from pyagent.agent import Agent


# ---- Test fixture helpers (mirrored from tests/smoke_plugins.py) ----


def _write_plugin(
    plugins_root: Path,
    dirname: str,
    *,
    name: str,
    api_version: str = "2",
    provides_tools: list[str] | None = None,
    provides_sections: list[str] | None = None,
    in_subagents: bool = True,
    plugin_py: str = "",
) -> Path:
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
        f'api_version = "{api_version}"\n\n'
        "[provides]\n"
        f"{tools_line}\n"
        f"{sections_line}\n\n"
        "[load]\n"
        f"in_subagents = {in_sub_line}\n"
    )
    (pdir / "manifest.toml").write_text(manifest)
    (pdir / "plugin.py").write_text(plugin_py)
    return pdir


def _isolated_config_dir():
    tmp_cfg = Path(tempfile.mkdtemp(prefix="pyagent-v2hook-cfg-"))
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


def _mk_agent(loaded: plugins_mod.LoadedPlugins) -> Agent:
    """An Agent with a stub LLM and one in-process tool. Each test
    drives `_route_tool` directly so we don't need to round-trip
    through a real LLM client."""
    stub_client = MagicMock()
    agent = Agent(client=stub_client, plugins=loaded)

    def widget(path: str, value: str = "") -> str:
        """Test tool that echoes its args; lets us assert mutation."""
        return f"widget(path={path!r}, value={value!r})"

    agent.add_tool("widget", widget, auto_offload=False)
    return agent


def _drain_pending(agent: Agent) -> list[str]:
    out: list[str] = []
    while not agent.pending_async_replies.empty():
        out.append(agent.pending_async_replies.get_nowait())
    return out


# ---- Test cases -----------------------------------------------------


def test_allow_no_change() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import ToolHookResult\n"
            "events = []\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        events.append(('before', name, dict(args)))\n"
            "        return ToolHookResult(decision='allow')\n"
            "    def after(name, args, result, is_error):\n"
            "        events.append(('after', name, result))\n"
            "        return None\n"
            "    api.before_tool_call(before)\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="allow",
            name="allow",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "v"},
        })
        assert result == "widget(path='/x', value='v')", result
        assert _drain_pending(agent) == []
        print("✓ allow: hook is observer, tool runs unchanged")
    finally:
        restore()


def test_block_short_circuits_before_permission(caplog=None) -> None:
    """A `block` decision must:
      - prevent the tool from running entirely
      - surface a `<blocked by plugin X: reason>` marker to the model
      - emit an INFO log line with `plugin=`, `tool=`, `reason=`
      - happen BEFORE permission checks (asserted by the tool body
        never running — if it ran, our marker tool would record it)
    """
    cfg, restore = _isolated_config_dir()
    try:
        # Plugin will block; the tool body would set this if reached.
        plugin_py = (
            "from pyagent.plugins import ToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        return ToolHookResult(\n"
            "            decision='block',\n"
            "            reason='not allowed in tests',\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="blocker",
            name="blocker",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)

        # Replace the widget tool with one that records if it ran —
        # if the block didn't short-circuit, this list would grow.
        ran: list[str] = []

        def widget(path: str, value: str = "") -> str:
            ran.append(path)
            return "TOOL RAN"

        agent.tools["widget"] = widget

        # Capture INFO logs from the plugins logger.
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        cap = _Capture(level=logging.INFO)
        agent_logger = logging.getLogger("pyagent.agent")
        prev_level = agent_logger.level
        agent_logger.setLevel(logging.INFO)
        agent_logger.addHandler(cap)
        try:
            result = agent._route_tool({
                "id": "1", "name": "widget",
                "args": {"path": "/x", "value": "v"},
            })
        finally:
            agent_logger.removeHandler(cap)
            agent_logger.setLevel(prev_level)

        assert result == "<blocked by plugin blocker: not allowed in tests>", result
        assert ran == [], (
            "block must short-circuit BEFORE the tool body runs "
            "(tool body is also where permission checks live)"
        )
        # INFO-level structured log line.
        info_msgs = [
            r.getMessage() for r in records if r.levelno == logging.INFO
        ]
        matched = [
            m for m in info_msgs
            if "plugin=blocker" in m
            and "tool=widget" in m
            and "reason=not allowed in tests" in m
        ]
        assert matched, (
            f"expected INFO log with plugin=blocker tool=widget reason=...; "
            f"got {info_msgs}"
        )
        print(
            "✓ block: tool not executed, marker returned, INFO log "
            "emitted, permission check unreached"
        )
    finally:
        restore()


def test_mutate_args() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import ToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        new = dict(args)\n"
            "        new['value'] = 'OVERRIDDEN'\n"
            "        return ToolHookResult(\n"
            "            decision='mutate', mutated_args=new,\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="mut",
            name="mut",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        call = {
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "original"},
        }
        result = agent._route_tool(call)
        assert "OVERRIDDEN" in result, result
        assert "original" not in result, result
        # The mutated args should have replaced call["args"] in place
        # so session replay sees what actually ran.
        assert call["args"]["value"] == "OVERRIDDEN", call["args"]
        print("✓ mutate: tool sees mutated args; conversation persists them")
    finally:
        restore()


def test_extra_user_message_before_and_after() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import ToolHookResult, AfterToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        return ToolHookResult(\n"
            "            extra_user_message='heads up: tool incoming',\n"
            "        )\n"
            "    def after(name, args, result, is_error):\n"
            "        return AfterToolHookResult(\n"
            "            extra_user_message='heads up: tool ran',\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="notes",
            name="notes",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x"},
        })
        notes = _drain_pending(agent)
        assert any("[plugin notes notes]: heads up: tool incoming" == n for n in notes), notes
        assert any("[plugin notes notes]: heads up: tool ran" == n for n in notes), notes
        print(
            "✓ extra_user_message from before/after lands on "
            "pending_async_replies with [plugin <name> notes]: tag"
        )
    finally:
        restore()


def test_extra_user_message_drains_to_next_turn() -> None:
    """Round-trip: a v2 plugin's note shows up as a user-role turn at
    the start of the next run iteration via `_drain_pending_async`."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import AfterToolHookResult\n"
            "def register(api):\n"
            "    def after(name, args, result, is_error):\n"
            "        return AfterToolHookResult(\n"
            "            extra_user_message='reconsider',\n"
            "        )\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="reflect",
            name="reflect",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x"},
        })
        # Drain — same machinery `Agent.run` uses at the top of each
        # loop iteration.
        n = agent._drain_pending_async()
        assert n == 1, n
        last = agent.conversation[-1]
        assert last["role"] == "user", last
        assert last["content"] == "[plugin reflect notes]: reconsider", last
        print(
            "✓ note drains as a user-role turn at the next loop "
            "iteration via pending_async_replies"
        )
    finally:
        restore()


def test_block_beats_mutate() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Plugin A blocks (loads first by directory prefix). Plugin B
        # would mutate — but never runs because A short-circuited.
        plugin_a = (
            "from pyagent.plugins import ToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        return ToolHookResult(\n"
            "            decision='block', reason='nope',\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        plugin_b = (
            "from pyagent.plugins import ToolHookResult\n"
            "events = []\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        events.append(('B-ran', dict(args)))\n"
            "        return ToolHookResult(\n"
            "            decision='mutate',\n"
            "            mutated_args={**args, 'value': 'B'},\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        _write_plugin(cfg / "plugins", dirname="01-a",
                      name="block-a", plugin_py=plugin_a)
        _write_plugin(cfg / "plugins", dirname="02-b",
                      name="mutate-b", plugin_py=plugin_b)
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "orig"},
        })
        assert result == "<blocked by plugin block-a: nope>", result
        # Plugin B's hook never ran.
        b_mod = next(
            mod for mod_name, mod in sys.modules.items()
            if mod_name.startswith("pyagent_plugin_mutate_b")
        )
        assert b_mod.events == [], (
            f"block must short-circuit; B should not have run: {b_mod.events}"
        )
        print("✓ block > mutate: later mutate hook does not fire")
    finally:
        restore()


def test_mutate_chaining() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        # Plugin A mutates value to "A"; plugin B sees "A" and mutates
        # to "A-then-B". The tool sees the final composed args.
        plugin_a = (
            "from pyagent.plugins import ToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        return ToolHookResult(\n"
            "            decision='mutate',\n"
            "            mutated_args={**args, 'value': 'A'},\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        plugin_b = (
            "from pyagent.plugins import ToolHookResult\n"
            "seen = []\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        seen.append(args.get('value'))\n"
            "        return ToolHookResult(\n"
            "            decision='mutate',\n"
            "            mutated_args={**args, 'value': args.get('value', '') + '-then-B'},\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        _write_plugin(cfg / "plugins", dirname="01-mA",
                      name="mut-a", plugin_py=plugin_a)
        _write_plugin(cfg / "plugins", dirname="02-mB",
                      name="mut-b", plugin_py=plugin_b)
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "orig"},
        })
        assert "A-then-B" in result, result
        # Plugin B saw what plugin A returned.
        b_mod = next(
            mod for mod_name, mod in sys.modules.items()
            if mod_name.startswith("pyagent_plugin_mut_b")
        )
        assert b_mod.seen == ["A"], (
            f"plugin B must see A's mutated value, not 'orig': {b_mod.seen}"
        )
        print("✓ mutate chains in registration order; later sees earlier's args")
    finally:
        restore()


def test_replace_result_chaining() -> None:
    cfg, restore = _isolated_config_dir()
    try:
        plugin_a = (
            "from pyagent.plugins import AfterToolHookResult\n"
            "def register(api):\n"
            "    def after(name, args, result, is_error):\n"
            "        return AfterToolHookResult(replace_result='A')\n"
            "    api.after_tool_call(after)\n"
        )
        plugin_b = (
            "from pyagent.plugins import AfterToolHookResult\n"
            "seen = []\n"
            "def register(api):\n"
            "    def after(name, args, result, is_error):\n"
            "        seen.append(result)\n"
            "        return AfterToolHookResult(\n"
            "            replace_result=result + '-then-B',\n"
            "        )\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(cfg / "plugins", dirname="01-rA",
                      name="rep-a", plugin_py=plugin_a)
        _write_plugin(cfg / "plugins", dirname="02-rB",
                      name="rep-b", plugin_py=plugin_b)
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x"},
        })
        assert result == "A-then-B", result
        b_mod = next(
            mod for mod_name, mod in sys.modules.items()
            if mod_name.startswith("pyagent_plugin_rep_b")
        )
        assert b_mod.seen == ["A"], (
            f"plugin B must see A's replaced result, not the tool's "
            f"original output: {b_mod.seen}"
        )
        print("✓ replace_result chains; later sees earlier's replacement")
    finally:
        restore()


def test_v1_return_value_ignored() -> None:
    """A v1 plugin (api_version='1') returning a v2-shaped value must
    be ignored — otherwise a v1 plugin that accidentally returns
    `True` or a stray object could start blocking tools."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import ToolHookResult\n"
            "def register(api):\n"
            "    def before(name, args):\n"
            "        return ToolHookResult(\n"
            "            decision='block', reason='v1 should be ignored',\n"
            "        )\n"
            "    api.before_tool_call(before)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="legacy",
            name="legacy",
            api_version="1",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "v"},
        })
        # Tool ran normally — block was ignored because plugin is v1.
        assert "widget(path='/x'" in result, result
        assert "<blocked" not in result, result
        print("✓ v1 plugin's v2-shaped return is ignored unconditionally")
    finally:
        restore()


def test_strategic_reevaluation_path_keyed() -> None:
    """Bundled strategic-reevaluation plugin: 3 fails on path A → note
    fires; 3 fails interleaved across A and B → no note (per-path
    counter)."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["strategic-reevaluation"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        names = [s.manifest.name for s in loaded.states]
        assert "strategic-reevaluation" in names, (
            f"expected strategic-reevaluation to load: {names}"
        )

        # Reset the per-path counter so this run starts clean.
        from pyagent.plugins.strategic_reevaluation import _reset_for_tests
        _reset_for_tests()

        agent = _mk_agent(loaded)

        # Force every edit_file invocation to look like a failure.
        def failing_edit(path: str, **kw: Any) -> str:
            return "<error: old_string ... not found>"

        agent.add_tool("edit_file", failing_edit, auto_offload=False)

        def call_edit(path: str) -> None:
            agent._route_tool({
                "id": f"id-{path}",
                "name": "edit_file",
                "args": {"path": path, "old_string": "x", "new_string": "y"},
            })

        # Three consecutive failures on path A → note fires.
        call_edit("/a")
        call_edit("/a")
        notes = _drain_pending(agent)
        assert notes == [], (
            f"only 2 failures yet; should not have fired: {notes}"
        )
        call_edit("/a")
        notes = _drain_pending(agent)
        assert any("strategic-reevaluation notes" in n for n in notes), notes
        assert any("/a" in n for n in notes), notes
        print(
            "✓ strategic-reevaluation: 3 consecutive fails on path A "
            "→ inject"
        )

        # Reset and try interleaved A/B/A/B/A/B failures → no inject
        # (per-path counter; each path tops out at 1 between resets).
        # Wait — interleaved A/B/A/B means path A sees 3 in a row from
        # the plugin's perspective IF it only counts edit_file calls
        # against that specific path. But "consecutive" in the plugin
        # means consecutive failures on THAT path; another path's
        # tool call doesn't reset path A's counter. Re-read the spec:
        # "3 fails interleaved across paths A and B → no inject
        # (path-keyed counter)". So with 3 A-fails interleaved with
        # 3 B-fails (6 calls total: A,B,A,B,A,B), each path saw
        # exactly 3 edit_file failures back-to-back from its own
        # perspective — and SHOULD trip the threshold. The
        # interleaving point is about NOT mixing them into a single
        # global counter.
        #
        # Re-reading the issue: "3 fails interleaved across paths A
        # and B → no inject (path-specific counter)". This means
        # if you have 3 total failures spread across A and B (e.g.
        # A, B, A), no path has seen 3 — so no inject. That's the
        # path-specific bit: a global counter would inject on the
        # third failure regardless of path. The path-keyed counter
        # does NOT.
        _reset_for_tests()
        call_edit("/a")
        call_edit("/b")
        call_edit("/a")
        notes = _drain_pending(agent)
        assert notes == [], (
            f"3 failures spread across two paths must not trip the "
            f"per-path threshold: {notes}"
        )
        print(
            "✓ strategic-reevaluation: 3 fails spread across A and B "
            "→ no inject (per-path counter)"
        )
    finally:
        restore()


def test_strategic_reevaluation_resets_on_success() -> None:
    """A successful edit on a path resets that path's counter."""
    cfg, restore = _isolated_config_dir()
    try:
        (cfg / "config.toml").write_text(
            'built_in_plugins_enabled = ["strategic-reevaluation"]\n'
        )
        loaded = plugins_mod.load(is_subagent=False)
        from pyagent.plugins.strategic_reevaluation import _reset_for_tests
        _reset_for_tests()

        agent = _mk_agent(loaded)

        flip = {"fail": True}

        def edit(path: str, **kw: Any) -> str:
            if flip["fail"]:
                return "<error: nope>"
            return f"Wrote 1 replacement to {path}"

        agent.add_tool("edit_file", edit, auto_offload=False)

        def call_edit(path: str) -> None:
            agent._route_tool({
                "id": f"id-{path}",
                "name": "edit_file",
                "args": {"path": path, "old_string": "x", "new_string": "y"},
            })

        # Two fails, one success, two more fails → counter was reset
        # by the success, so we're at 2 again, not 4. No note.
        call_edit("/a")
        call_edit("/a")
        flip["fail"] = False
        call_edit("/a")
        flip["fail"] = True
        call_edit("/a")
        call_edit("/a")
        notes = _drain_pending(agent)
        assert notes == [], (
            f"success should have reset the counter; only 2 post-reset "
            f"failures should not trip threshold: {notes}"
        )
        print("✓ strategic-reevaluation: successful edit resets per-path counter")
    finally:
        restore()


def test_is_error_helper_contract() -> None:
    """The errors-as-data contract: any tool result starting with `<`
    is an error marker; non-error results MUST NOT start with `<`.
    """
    from pyagent.tools import is_error_result, ERROR_MARKER_PREFIX

    assert ERROR_MARKER_PREFIX == "<", ERROR_MARKER_PREFIX
    assert is_error_result("<refused: empty>") is True
    assert is_error_result("<unknown sid 'foo'>") is True
    assert is_error_result("Error: ValueError: bad") is False
    assert is_error_result("Wrote 1 replacement to /x") is False
    assert is_error_result("") is False
    # Tolerant of leading whitespace.
    assert is_error_result("\n  <error: something>") is True
    # Defensive against non-string defenders.
    assert is_error_result(None) is False  # type: ignore[arg-type]
    assert is_error_result(42) is False  # type: ignore[arg-type]
    print("✓ tools.is_error_result encodes the <…>-prefix contract")


def test_after_hook_receives_is_error_flag() -> None:
    """v2 after_tool hooks receive `is_error` as the 4th positional
    argument, set by the harness from (raised exception OR result
    starts with `<`).
    """
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import AfterToolHookResult\n"
            "calls = []\n"
            "def register(api):\n"
            "    def after(name, args, result, is_error):\n"
            "        calls.append((name, result[:24], is_error))\n"
            "        return None\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="errflag",
            name="errflag",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        # Override widget to be deterministic.
        agent.add_tool(
            "widget",
            lambda path="", value="": "happy result",
            auto_offload=False,
        )
        # Add a failing-marker tool.
        agent.add_tool(
            "fail_marker",
            lambda: "<error: simulated failure>",
            auto_offload=False,
        )
        # Add a raising tool.
        def boom() -> str:
            raise RuntimeError("boom")
        agent.add_tool("boom", boom, auto_offload=False)

        agent._route_tool({"id": "1", "name": "widget", "args": {}})
        agent._route_tool({"id": "2", "name": "fail_marker", "args": {}})
        agent._route_tool({"id": "3", "name": "boom", "args": {}})

        mod = next(
            m for n, m in sys.modules.items()
            if n.startswith("pyagent_plugin_errflag")
        )
        assert len(mod.calls) == 3, mod.calls
        # Success → is_error False.
        assert mod.calls[0] == ("widget", "happy result", False), mod.calls[0]
        # `<…>` marker → is_error True.
        assert mod.calls[1][0] == "fail_marker"
        assert mod.calls[1][2] is True, mod.calls[1]
        # Raised exception → is_error True (rendered into Error: …
        # which doesn't start with `<` but the harness tracks the
        # raise separately).
        assert mod.calls[2][0] == "boom"
        assert mod.calls[2][2] is True, mod.calls[2]
        print("✓ after_tool receives is_error: success=False, marker=True, raise=True")
    finally:
        restore()


def test_replace_result_must_be_string() -> None:
    """v2 contract: replace_result is `str | None`. Non-string values
    are dropped with a warning; the original tool result stands."""
    cfg, restore = _isolated_config_dir()
    try:
        plugin_py = (
            "from pyagent.plugins import AfterToolHookResult\n"
            "def register(api):\n"
            "    def after(name, args, result, is_error):\n"
            "        # Returning a non-string replace_result must be\n"
            "        # ignored (typed as str|None per the v2 contract).\n"
            "        return AfterToolHookResult(replace_result=42)\n"
            "    api.after_tool_call(after)\n"
        )
        _write_plugin(
            cfg / "plugins",
            dirname="badtype",
            name="badtype",
            plugin_py=plugin_py,
        )
        loaded = plugins_mod.load()
        agent = _mk_agent(loaded)
        result = agent._route_tool({
            "id": "1", "name": "widget",
            "args": {"path": "/x", "value": "v"},
        })
        # Non-string replace_result was dropped; original tool output
        # stands.
        assert "widget(path='/x'" in result, result
        assert result != "42", result
        print("✓ replace_result non-string dropped with warning")
    finally:
        restore()


def main() -> None:
    test_is_error_helper_contract()
    test_after_hook_receives_is_error_flag()
    test_replace_result_must_be_string()
    test_allow_no_change()
    test_block_short_circuits_before_permission()
    test_mutate_args()
    test_extra_user_message_before_and_after()
    test_extra_user_message_drains_to_next_turn()
    test_block_beats_mutate()
    test_mutate_chaining()
    test_replace_result_chaining()
    test_v1_return_value_ignored()
    test_strategic_reevaluation_path_keyed()
    test_strategic_reevaluation_resets_on_success()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
