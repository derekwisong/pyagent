"""Smoke for `pyagent-bench`'s default-budget-by-model table.

Locks the per-model defaults so a future model rename / pricing-table
update doesn't silently make Opus runs halt at the Sonnet budget (or
vice versa). Doesn't run the bench — just checks the resolver.

Run with:
    .venv/bin/python -m tests.smoke_bench_defaults
"""

from __future__ import annotations

from pyagent.bench_cli import _BUDGET_FALLBACK_USD, _default_budget_for


def _check_anthropic_tiers() -> None:
    # Opus is the expensive model; default budget is sized so a typical
    # research scenario completes without halting.
    assert _default_budget_for("anthropic/claude-opus-4-7") == 3.00
    # Sonnet is the middle tier; matches the historical bench default.
    assert _default_budget_for("anthropic/claude-sonnet-4-6") == 0.50
    # Haiku is cheap; smaller cap so a runaway Haiku run can't quietly
    # cost more than the user expected.
    assert (
        _default_budget_for("anthropic/claude-haiku-4-5-20251001") == 0.20
    )
    print(
        "✓ Anthropic per-tier budgets: Opus $3.00 / Sonnet $0.50 / Haiku $0.20"
    )


def _check_other_providers() -> None:
    # OpenAI gpt-4o is in the table at $0.50; gpt-4o-mini at $0.10.
    assert _default_budget_for("openai/gpt-4o") == 0.50
    assert _default_budget_for("openai/gpt-4o-mini") == 0.10
    # Gemini in the table.
    assert _default_budget_for("gemini/gemini-2.5-flash") == 0.10
    print(
        f"✓ Non-Anthropic budgets: gpt-4o $0.50 / gpt-4o-mini $0.10 / "
        f"gemini-flash $0.10"
    )


def _check_unknown_model_falls_back() -> None:
    # Unknown model → fallback. Don't crash on a future model name.
    assert _default_budget_for("unknown/some-future-model") == _BUDGET_FALLBACK_USD
    assert _default_budget_for("") == _BUDGET_FALLBACK_USD
    print(f"✓ unknown model falls back to ${_BUDGET_FALLBACK_USD:.2f}")


def main() -> None:
    _check_anthropic_tiers()
    _check_other_providers()
    _check_unknown_model_falls_back()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
