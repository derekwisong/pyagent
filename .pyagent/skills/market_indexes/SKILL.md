---
name: market_indexes
description: Pull global stock-index, sector, and thematic-asset performance from Yahoo Finance for big-picture market analysis.
---

# Market indexes

Quick read on global markets. Hits the unofficial Yahoo Finance
chart endpoint (`query1.finance.yahoo.com/v8/finance/chart/`) — no
key, but Yahoo asks for a browser-like User-Agent (the script sets
one). Built for "what's happening in markets right now" framing,
not for backtests or bar-by-bar data.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...`.

Subcommands:

- `quote <symbol> [<symbol> ...] [--periods 1d,5d,1m,3m,ytd,1y]` —
  one row per symbol with current price and percent change over each
  requested period. Symbols use Yahoo conventions
  (`^GSPC`, `^FTSE`, `XLK`, `BTC-USD`). All requests run in parallel.
- `globe [--periods ...]` — convenience: `quote` over a curated list
  of major global indexes (US, EU, UK, Japan, China, India, Brazil,
  etc.). Use this for "snapshot of world markets."
- `sectors [--periods ...]` — `quote` over the SPDR US sector ETFs
  (XLK/XLF/XLE/XLV/XLI/XLY/XLP/XLU/XLB/XLRE/XLC). Reveals what's
  leading and lagging within the US tape.
- `themes [--periods ...]` — `quote` over thematic / cross-asset
  indicators: VIX, dollar index, 10-year yield, gold, crude oil,
  bitcoin. Use this to frame the *why* behind index moves.
- `symbols` — list the preset symbol groups (`globe`, `sectors`,
  `themes`) so you can see what each convenience subcommand pulls.

`--periods` is a comma list. Defaults to `1d,5d,1m,3m,ytd,1y`.
Supported tokens: `1d`, `5d`, `1m`, `3m`, `6m`, `ytd`, `1y`, `5y`.

## Notes for the agent

- Yahoo data is best-effort — the unofficial endpoint is widely used
  but has no SLA. If a symbol comes back with `error`, surface it
  rather than silently dropping; the user usually wants to know which
  ticker failed to resolve.
- **Rate limiting:** Yahoo throttles bursts (>~5 req/sec) per IP and
  the cooldown can be several minutes once tripped. The script caps
  parallelism at 4 and retries 429 once with backoff. If a whole
  preset comes back with `status=429`, the IP is in cooldown — wait
  a few minutes before retrying rather than hammering. Don't chain
  `globe`+`sectors`+`themes` back-to-back on a fresh session; a
  single preset (~6–17 symbols) is well within budget but three in
  a row can trip the limit.
- Period changes are computed from daily close history pulled in one
  call (`range=5y&interval=1d`), so all periods come from one
  request per symbol. `1d` is last close vs prior close — not
  intraday — so on a trading day mid-session this lags the live
  ticker.
- Dollar moves matter for international comparisons: a German index
  up 5% in EUR is roughly flat in USD if the dollar is up 5%. When
  the user is comparing across regions, mention DXY (`themes` has
  it) before drawing conclusions.
- The Yahoo session sometimes 401s on first call until a cookie/crumb
  is in place. The script does not implement crumb negotiation —
  if you see persistent 401s, it's likely Yahoo tightened auth and
  the script needs an update; flag it rather than retry-looping.

## Analyst framing — turning numbers into a story

When the user asks "what's happening in markets," don't just dump
the JSON. Lead with the shape:

**Risk regime (use `themes`):**
- VIX `<15` — complacent / low-vol
- VIX `15–20` — normal
- VIX `20–30` — stressed
- VIX `>30` — panic / dislocation
- 10-year yield direction matters more than level: rising long-end =
  duration pain (tech, REITs hit hardest), falling = bid for duration
- DXY rising = USD strength = headwind for emerging markets, gold,
  US multinationals' overseas earnings

**US sector rotation (use `sectors`):**
- XLK + XLY leading = risk-on / growth bid
- XLP + XLU + XLV leading = defensive / late-cycle
- XLF leading = curve steepening / financial-conditions easing
- XLE leading + crude up = supply story; XLE leading + crude flat =
  capital discipline / dividend chase
- Watch *dispersion* (best minus worst): tight = consensus market,
  wide = thematic regime

**Global breadth (use `globe`):**
- US-only strength + EM/EU weak = strong-dollar environment
- Synchronized global up = global liquidity / earnings cycle
- Asia diverging from West usually = China policy shift or yen carry
- Compare YTD to 1y to see whether the year's return was front- or
  back-loaded — that changes the narrative meaningfully

**Headline framing:**
- A single-day move under ±0.5% on a major index is noise; don't
  build a thesis on it.
- A weekly move of ±2% is meaningful; ±5% is regime-significant.
- Always cross-check with VIX before calling something a "rout" —
  a 1% S&P drop with VIX flat is not the same story as 1% with VIX
  up 30%.

## Typical flows

- "How are markets today?" →
  `cli.py globe`, then `cli.py themes`, narrate the regime.
- "Tech vs everything else" →
  `cli.py sectors`, lead with XLK vs XLP/XLU spread.
- "What does Japan look like vs US?" →
  `cli.py quote ^N225 ^GSPC`, then mention USDJPY context if
  drawing currency-adjusted conclusions.
- "Big picture this year" →
  `cli.py globe --periods ytd,1y` paired with
  `cli.py themes --periods ytd,1y` for the cross-asset frame.
