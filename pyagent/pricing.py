"""Provider-agnostic pricing tables and cost estimation.

Extracted from `pyagent.cli` so consumers that only need cost math
(the sessions audit, the bench harness) don't have to drag click /
readline / rich into their import path.

Pricing here is a best-effort estimate, not authoritative billing —
update freely as providers change their rates.
"""

from __future__ import annotations

from pyagent import llms


# USD per million tokens, (input, output). Models not listed get
# token-only display, no $ amount. Update freely as pricing changes.
PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gemini-2.5-flash": (0.075, 0.30),
}

# Anthropic ephemeral-cache pricing multipliers applied to the model's
# base input rate: writes are 1.25× input, reads are 0.1× input.
ANTHROPIC_CACHE_WRITE_MULT = 1.25
ANTHROPIC_CACHE_READ_MULT = 0.1


def is_anthropic_model(name: str) -> bool:
    return name.startswith("claude-")


def model_name(model_str: str) -> str:
    """Extract the bare model name from a 'provider/name' string.

    Falls back to the provider's default model (via the llms registry)
    if no `/name` was given so the pricing lookup still works on
    `--model anthropic`.
    """
    _, _, name = llms.resolve_model(model_str).partition("/")
    return name


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float | None:
    """USD cost estimate, or None if the model isn't in the pricing
    table. Falls back gracefully on unknown / future models.

    Anthropic ephemeral-cache writes/reads are billed at multiples of
    the base input rate. OpenAI and Gemini surface cached counts but
    bill them inside `prompt_tokens` / `prompt_token_count` at the
    regular input rate, so no separate adjustment is needed for those.
    """
    name = model_name(model)
    if not name:
        return None
    rates = PRICING_USD_PER_MTOK.get(name)
    if rates is None:
        return None
    in_rate, out_rate = rates
    cost = input_tokens * in_rate + output_tokens * out_rate
    if is_anthropic_model(name):
        cost += cache_creation_tokens * in_rate * ANTHROPIC_CACHE_WRITE_MULT
        cost += cache_read_tokens * in_rate * ANTHROPIC_CACHE_READ_MULT
    return cost / 1_000_000


def format_usage_suffix(
    input_tokens: int,
    output_tokens: int,
    model: str,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> str:
    """Build the ` [Nk tok / $0.0X]` suffix for the status footer.

    Empty string when there's nothing to show (no LLM calls yet).
    On Anthropic the four token counts (input, output, cache writes,
    cache reads) are disjoint — `input_tokens` excludes both cache
    reads and writes — so the displayed total bundles all four. On
    OpenAI / Gemini the providers' "input" already includes their
    cached-token count; bundling cache_read on top would double-count
    the same tokens in the displayed total. Cost estimation in
    `estimate_cost_usd` already gates the cache-pricing multipliers
    to Anthropic; this gate keeps the displayed token count honest the
    same way.
    """
    name = model_name(model)
    if is_anthropic_model(name):
        total = (
            input_tokens
            + output_tokens
            + cache_creation_tokens
            + cache_read_tokens
        )
    else:
        total = input_tokens + output_tokens
    if total == 0:
        return ""
    if total >= 1000:
        tok_str = f"{total / 1000:.1f}k tok"
    else:
        tok_str = f"{total} tok"
    cost = estimate_cost_usd(
        model,
        input_tokens,
        output_tokens,
        cache_creation_tokens,
        cache_read_tokens,
    )
    if cost is None:
        return f" [{tok_str}]"
    if cost < 0.01:
        cost_str = f"${cost:.4f}"
    else:
        cost_str = f"${cost:.3f}"
    return f" [{tok_str} / {cost_str}]"
