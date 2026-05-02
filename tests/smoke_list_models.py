"""End-to-end smoke for `pyagent --list-models` and the
`ProviderSpec.list_models` protocol it sits on top of.

Concerns:

  1. **Built-in providers carry hardcoded canonical lists.** Each
     of anthropic / openai / gemini / pyagent must return a non-empty
     `list[str]` from `list_models()` and include its own
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

Run with:

    .venv/bin/python -m tests.smoke_list_models
"""

from __future__ import annotations

from pathlib import Path

from pyagent import llms, plugins


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_builtin_canonical_lists() -> None:
    """Each built-in returns a non-empty list that includes its
    default — the CLI relies on the default to render `(default)`."""
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
            f"{spec.name} default ({spec.default_model!r}) is in its own list",
            spec.default_model in models,
            f"default={spec.default_model!r} list={models}",
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
    sentinel_models = ["alpha", "beta"]
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


def main() -> None:
    _check_builtin_canonical_lists()
    _check_aggregator_walks_both_pools()
    _check_failing_provider_renders_error()
    _check_plugin_list_models_hook_plumbs_through()
    print("smoke_list_models: all checks passed")


if __name__ == "__main__":
    main()
