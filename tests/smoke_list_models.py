"""End-to-end smoke for `pyagent --list-models` and the
`ProviderSpec.list_models` protocol it sits on top of.

Concerns:

  1. **Built-in providers carry hardcoded canonical lists.** Each
     of anthropic / openai / gemini / pyagent must return a non-empty
     `list[ModelInfo]` from `list_models()` and include its own
     `default_model` so the CLI can flag the active default.
  2. **Aggregator surfaces both pools.** `llms.list_all_models()`
     walks built-ins in PROVIDERS order, then plugin providers in
     registration order. Plugins without a `list_models` callable
     surface as a `ProviderListing` with empty `models`.
  3. **Per-provider failures don't kill the listing.** A provider
     whose `list_models` raises produces a `ProviderListing` with
     `error=<reason>` and the rest of the catalog still renders.
  4. **Plugin hook plumbs through.** `register_provider(...,
     list_models=...)` from a plugin lands on the resulting
     `ProviderSpec` after `plugins.load()`.
  5. **CLI prints and exits 0.** `pyagent --list-models` invokes
     the renderer, marks defaults, surfaces per-model capability
     tags, and never reaches model resolution / session start.
     Tested via click's CliRunner with no env keys present so a
     normal launch would have errored on missing keys — proves the
     exit short-circuits before resolution.

Run with:

    .venv/bin/python -m tests.smoke_list_models
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from pyagent import (
    cli as cli_mod,
    llms,
    paths as paths_mod,
    plugins,
)


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_builtin_canonical_lists() -> None:
    """Each built-in returns a non-empty list of `ModelInfo` records
    that includes its default — the CLI relies on the default to
    render `(default)`."""
    for spec in llms.PROVIDERS:
        _check(
            f"{spec.name} has list_models callable",
            spec.list_models is not None,
        )
        models = spec.list_models()
        _check(
            f"{spec.name} list non-empty",
            isinstance(models, list) and len(models) > 0,
            repr(models),
        )
        _check(
            f"{spec.name} entries are ModelInfo",
            all(isinstance(m, llms.ModelInfo) for m in models),
            repr(models),
        )
        names = [m.name for m in models]
        _check(
            f"{spec.name} default ({spec.default_model!r}) is in its own list",
            spec.default_model in names,
            f"default={spec.default_model!r} list={names}",
        )


def _check_aggregator_walks_both_pools() -> None:
    """`list_all_models` returns built-ins in PROVIDERS order, then
    plugin providers — and includes every loaded plugin provider."""
    plugins.load()
    listings = llms.list_all_models()
    names = [l.name for l in listings]

    builtin_names = [p.name for p in llms.PROVIDERS]
    _check(
        "built-ins precede plugins in aggregator output",
        names[: len(builtin_names)] == builtin_names,
        repr(names),
    )
    for plugin_provider in llms._PLUGIN_PROVIDERS:
        _check(
            f"plugin provider {plugin_provider!r} present in aggregator",
            plugin_provider in names,
            repr(names),
        )


def _check_failing_provider_renders_error() -> None:
    """A `list_models` callable that raises must surface as a
    `ProviderListing.error` so the CLI can render `(unavailable: ...)`
    without nuking the whole listing."""

    def boom() -> list[str]:
        raise RuntimeError("kaboom")

    fake_spec = llms.ProviderSpec(
        name="brokenly",
        env_vars=(),
        default_model="x",
        factory=lambda **kw: None,
        list_models=boom,
    )
    listing = llms._listing_for(fake_spec)
    _check(
        "failing list_models → ProviderListing.error populated",
        listing.error == "kaboom" and listing.models == (),
        repr(listing),
    )

    # The aggregator must keep going even when one provider's listing
    # raises — patch one built-in to fail and verify the rest still
    # appear.
    plugins.load()
    original = llms.PROVIDERS[0].list_models
    object.__setattr__(llms.PROVIDERS[0], "list_models", boom)
    try:
        listings = llms.list_all_models()
    finally:
        object.__setattr__(llms.PROVIDERS[0], "list_models", original)
    by_name = {l.name: l for l in listings}
    _check(
        "broken provider entry has error",
        by_name[llms.PROVIDERS[0].name].error == "kaboom",
        repr(by_name[llms.PROVIDERS[0].name]),
    )
    _check(
        "siblings still rendered after a sibling fails",
        all(
            l.name in by_name
            for l in listings
            if l.name != llms.PROVIDERS[0].name
        ),
    )


def _check_plugin_list_models_hook_plumbs_through() -> None:
    """A plugin that calls `register_provider(..., list_models=...)`
    sees that callable land on its `ProviderSpec` after load()."""
    from pyagent.plugins import (
        Manifest,
        PluginAPI,
        _PluginState,
    )

    m = Manifest(
        name="hooky",
        version="0",
        description="",
        api_version="1",
        provides_tools=(),
        provides_prompt_sections=(),
        provides_providers=("hooky",),
        requires_python="",
        requires_env=(),
        requires_binaries=(),
        in_subagents=True,
        source=Path("/dev/null"),
    )
    api = PluginAPI(_PluginState(manifest=m))
    sentinel_models = [
        llms.ModelInfo(name="alpha", capabilities=("tools",)),
        llms.ModelInfo(name="beta"),
    ]
    api.register_provider(
        "hooky",
        lambda **kw: None,
        default_model="alpha",
        list_models=lambda: sentinel_models,
    )
    rec = api._state.providers["hooky"]
    _check(
        "_RegisteredProvider carries list_models",
        rec.list_models is not None
        and rec.list_models() == sentinel_models,
        repr(rec),
    )


class _FakeResponse:
    """Minimal `requests.Response` stand-in used by the CLI fixture.

    The ollama client's `_raise_with_body` reads `ok` before
    deciding whether to raise, so we expose it here. Tests for the
    error-path live in `smoke_ollama_plugin.py`; this fixture only
    needs the success-shape surface.
    """

    status_code = 200
    ok = True

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload

    @property
    def text(self) -> str:
        import json as _json
        return _json.dumps(self._payload)

    def raise_for_status(self) -> None:
        return None


def _check_cli_prints_and_exits() -> None:
    """`pyagent --list-models` runs in a clean env and prints every
    built-in plus the loaded plugin providers — without going
    anywhere near `_resolve_model`. Test under an empty env so a
    normal launch would have UsageError'd on missing keys.

    The CLI runs its own `plugins.load()` internally, so we patch
    the underlying ``requests`` in the ollama client module — that
    way the live `list_models` callable resolves to a stable fixture
    no matter how many times the spec is rebuilt.

    Two mocked calls per ollama model: `/api/tags` (one GET) and one
    `/api/show` POST per model returned. The `/api/show` fixture
    populates a ``capabilities`` array so we can verify capability
    tags render in the CLI output and the no-tools warning fires for
    a model without ``tools`` in its capability list.
    """
    from pyagent.plugins.ollama import client as ollama_client_mod

    runner = CliRunner()
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-listmodels-"))
    fake_tags = _FakeResponse(
        {
            "models": [
                {"name": "fake-tools:1b"},
                {"name": "fake-vision:11b"},
            ]
        }
    )

    def fake_show(url, json=None, timeout=None):
        # Distinct capability arrays per model so the renderer can
        # exercise both the tool-capable and no-tools branches.
        name = (json or {}).get("name", "")
        caps = ["tools", "completion"] if "tools" in name else ["vision", "completion"]
        return _FakeResponse({"capabilities": caps})

    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            with mock.patch.object(
                ollama_client_mod.requests, "get", return_value=fake_tags
            ):
                with mock.patch.object(
                    ollama_client_mod.requests,
                    "post",
                    side_effect=fake_show,
                ):
                    # Empty env so the test doesn't depend on what's
                    # set in the host environment — also forces
                    # _resolve_host to fall back to localhost default.
                    result = runner.invoke(
                        cli_mod.main,
                        ["--list-models"],
                        env={
                            "ANTHROPIC_API_KEY": "",
                            "OPENAI_API_KEY": "",
                            "GEMINI_API_KEY": "",
                            "GOOGLE_API_KEY": "",
                            "OLLAMA_HOST": "",
                            "OLLAMA_MODEL": "",
                        },
                    )

    _check(
        "exit code 0",
        result.exit_code == 0,
        f"code={result.exit_code} output={result.output!r}",
    )
    out = result.output
    _check("header printed", "Available models" in out, out)
    _check("anthropic block printed", "anthropic" in out, out)
    _check("default tag printed somewhere", "(default)" in out, out)
    _check("plugin tag printed", "(plugin)" in out, out)
    _check(
        "stubbed ollama tool-capable model surfaces in output",
        "fake-tools:1b" in out,
        out,
    )
    _check(
        "stubbed ollama vision model surfaces in output",
        "fake-vision:11b" in out,
        out,
    )
    _check(
        "tools capability tag rendered",
        "(tools)" in out,
        out,
    )
    _check(
        "vision capability tag rendered",
        "(vision)" in out,
        out,
    )
    _check(
        "no-tools warning rendered for vision model",
        "no tools — chat only" in out,
        out,
    )
    _check(
        "boring `completion` capability filtered out of output",
        "completion" not in out,
        out,
    )


def main() -> None:
    _check_builtin_canonical_lists()
    _check_aggregator_walks_both_pools()
    _check_failing_provider_renders_error()
    _check_plugin_list_models_hook_plumbs_through()
    _check_cli_prints_and_exits()
    print("smoke_list_models: all checks passed")


if __name__ == "__main__":
    main()
