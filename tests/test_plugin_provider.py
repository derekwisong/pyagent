"""Smoke tests for the plugin-provider surface (PluginAPI.register_provider
+ pyagent.llms plugin-router integration).

What's covered:

  - Bundled `echo-plugin` registers and shows up in `LoadedPlugins.providers()`.
  - `pyagent.llms.set_plugin_providers` is invoked by `load()` so
    `get_client("echo-plugin/...")` resolves to the plugin's factory.
  - `resolve_model("echo-plugin")` fills in the plugin's default_model.
  - `auto_detect_provider` ignores plugin providers (per design — they're
    opt-in only).
  - The `[provides] providers = [...]` validation arm catches mismatches
    in both directions (declared-but-not-registered, registered-but-not-
    declared).
  - Conflicts with a built-in provider name raise at register time, not
    at call time.
"""

from __future__ import annotations

from pathlib import Path

from pyagent import llms
from pyagent.llms import (
    PROVIDERS,
    auto_detect_provider,
    get_client,
    resolve_model,
)
from pyagent.plugins import (
    Manifest,
    PluginAPI,
    _PluginState,
    _validate_provides,
    load,
)


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def test_bundled_echo_plugin_registers_provider() -> None:
    loaded = load()
    names = [s.manifest.name for s in loaded.states]
    _check("echo-plugin loaded", "echo-plugin" in names, repr(names))
    providers = loaded.providers()
    _check(
        "echo-plugin in LoadedPlugins.providers()",
        "echo-plugin" in providers,
        repr(list(providers)),
    )
    _check(
        "echo-plugin published to pyagent.llms._PLUGIN_PROVIDERS",
        "echo-plugin" in llms._PLUGIN_PROVIDERS,
    )


def test_get_client_resolves_plugin_provider() -> None:
    # Force a fresh load so we know the router state matches this run.
    load()
    client = get_client("echo-plugin/hello")
    _check(
        "client.provider_model is echo-plugin/hello",
        client.provider_model == "echo-plugin/hello",
        client.provider_model,
    )
    out = client.respond(
        conversation=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack", "tool_calls": []},
            {"role": "user", "content": "ECHO ME"},
        ],
    )
    _check("respond echoes latest user message", out["content"] == "ECHO ME", repr(out))
    _check("respond returns no tool_calls", out["tool_calls"] == [])


def test_resolve_model_fills_plugin_default() -> None:
    load()
    _check(
        "resolve_model('echo-plugin') → 'echo-plugin/echo'",
        resolve_model("echo-plugin") == "echo-plugin/echo",
        resolve_model("echo-plugin"),
    )


def test_auto_detect_excludes_plugin_providers() -> None:
    load()
    detected = auto_detect_provider()
    if detected is not None:
        _check(
            "auto_detect_provider never picks echo-plugin",
            detected.name != "echo-plugin",
            detected.name,
        )
    else:
        _check("auto_detect_provider returned None (no env keys set)", True)


def test_unknown_provider_error_lists_both_pools() -> None:
    load()
    try:
        get_client("nope/x")
    except ValueError as e:
        msg = str(e)
        _check("error mentions a built-in", "anthropic" in msg, msg)
        _check("error mentions echo-plugin", "echo-plugin" in msg, msg)
    else:
        _check("get_client('nope/x') raised", False)


def test_register_provider_conflict_with_builtin() -> None:
    m = Manifest(
        name="bogus",
        version="0",
        description="",
        api_version="1",
        provides_tools=(),
        provides_prompt_sections=(),
        provides_providers=("anthropic",),
        requires_python="",
        requires_env=(),
        requires_binaries=(),
        in_subagents=True,
        source=Path("/dev/null"),
    )
    api = PluginAPI(_PluginState(manifest=m))
    raised = False
    try:
        api.register_provider("anthropic", lambda **kw: None)
    except ValueError as e:
        raised = True
        _check(
            "conflict error mentions plugin name + 'built-in'",
            "bogus" in str(e) and "built-in" in str(e),
            str(e),
        )
    _check("register_provider raised on built-in collision", raised)


def test_register_provider_double_register_within_plugin() -> None:
    m = Manifest(
        name="dup",
        version="0",
        description="",
        api_version="1",
        provides_tools=(),
        provides_prompt_sections=(),
        provides_providers=("foo",),
        requires_python="",
        requires_env=(),
        requires_binaries=(),
        in_subagents=True,
        source=Path("/dev/null"),
    )
    api = PluginAPI(_PluginState(manifest=m))
    api.register_provider("foo", lambda **kw: None)
    raised = False
    try:
        api.register_provider("foo", lambda **kw: None)
    except ValueError:
        raised = True
    _check("double-registering same name raises", raised)


def test_validate_provides_catches_provider_mismatch() -> None:
    # Declared but never registered.
    m_missing = Manifest(
        name="missing-prov",
        version="0",
        description="",
        api_version="1",
        provides_tools=(),
        provides_prompt_sections=(),
        provides_providers=("ghost",),
        requires_python="",
        requires_env=(),
        requires_binaries=(),
        in_subagents=True,
        source=Path("/dev/null"),
    )
    state = _PluginState(manifest=m_missing)
    problem = _validate_provides(state)
    _check(
        "missing provider flagged",
        problem is not None and "ghost" in problem,
        repr(problem),
    )

    # Registered but never declared.
    m_extra = Manifest(
        name="extra-prov",
        version="0",
        description="",
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
    state2 = _PluginState(manifest=m_extra)
    PluginAPI(state2).register_provider("surprise", lambda **kw: None)
    problem2 = _validate_provides(state2)
    _check(
        "undeclared provider flagged",
        problem2 is not None and "surprise" in problem2,
        repr(problem2),
    )


def test_builtin_providers_unchanged() -> None:
    load()
    names = {p.name for p in PROVIDERS}
    _check(
        "built-in providers unchanged",
        names == {"anthropic", "openai", "gemini", "pyagent"},
        repr(names),
    )
    client = get_client("pyagent/echo")
    _check(
        "pyagent/echo still resolves",
        client.provider_model == "pyagent/echo",
        client.provider_model,
    )


def main() -> None:
    test_bundled_echo_plugin_registers_provider()
    test_get_client_resolves_plugin_provider()
    test_resolve_model_fills_plugin_default()
    test_auto_detect_excludes_plugin_providers()
    test_unknown_provider_error_lists_both_pools()
    test_register_provider_conflict_with_builtin()
    test_register_provider_double_register_within_plugin()
    test_validate_provides_catches_provider_mismatch()
    test_builtin_providers_unchanged()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
