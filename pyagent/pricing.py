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


def _format_token_count(total: int) -> str:
    if total >= 1000:
        return f"{total / 1000:.1f}k"
    return str(total)


def _format_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.3f}"


def gross_net_tokens(
    input_tokens: int,
    output_tokens: int,
    model: str,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> tuple[int, float]:
    """Return (gross_tokens, net_tokens) for the status footer.

    On Anthropic the four counts are disjoint — input excludes both
    cache reads and writes — so:
      - gross = input + output + cache_creation + cache_read
        (everything sent + got back; matches today's "tok" number)
      - net = input + output + cache_creation*1.25 + cache_read*0.1
        (cost-equivalent token count; weights match
        `estimate_cost_usd`'s Anthropic multipliers)

    On non-Anthropic providers, prompt_tokens already includes the
    cached count, so gross == net == input + output.
    """
    name = model_name(model)
    if is_anthropic_model(name):
        gross = (
            input_tokens
            + output_tokens
            + cache_creation_tokens
            + cache_read_tokens
        )
        net = (
            input_tokens
            + output_tokens
            + cache_creation_tokens * ANTHROPIC_CACHE_WRITE_MULT
            + cache_read_tokens * ANTHROPIC_CACHE_READ_MULT
        )
        return gross, net
    base = input_tokens + output_tokens
    return base, float(base)


def format_right_zone(
    input_tokens: int,
    output_tokens: int,
    model: str,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> tuple[str, str, str]:
    """Return (gross_str, net_str, cost_str) for the footer's right zone.

    Each component is a pre-formatted string ready to drop into the
    `gross / net · $cost` display. All three are empty strings when no
    LLM activity has happened yet (so the right zone can be omitted
    entirely on first launch).

    `cost_str` is `$0.00` rather than empty when pricing returns None
    for the model — the right zone is the contract; a missing dollar
    figure is a separate concern from "nothing to show".
    """
    gross, net = gross_net_tokens(
        input_tokens,
        output_tokens,
        model,
        cache_creation_tokens,
        cache_read_tokens,
    )
    if gross == 0:
        return "", "", ""
    gross_str = _format_token_count(gross)
    net_str = _format_token_count(int(round(net)))
    cost = estimate_cost_usd(
        model,
        input_tokens,
        output_tokens,
        cache_creation_tokens,
        cache_read_tokens,
    )
    cost_str = "$0.00" if cost is None else _format_cost(cost)
    return gross_str, net_str, cost_str


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
    gross, _ = gross_net_tokens(
        input_tokens,
        output_tokens,
        model,
        cache_creation_tokens,
        cache_read_tokens,
    )
    if gross == 0:
        return ""
    tok_str = f"{_format_token_count(gross)} tok"
    cost = estimate_cost_usd(
        model,
        input_tokens,
        output_tokens,
        cache_creation_tokens,
        cache_read_tokens,
    )
    if cost is None:
        return f" [{tok_str}]"
    return f" [{tok_str} / {_format_cost(cost)}]"
