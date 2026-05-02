---
name: housing_market
description: Research US housing market trends — metro home values, mortgage rates, and national indicators from Zillow Research and FRED.
---

# Housing market

Aggregate housing data from public sources, no auth required:

- **Zillow Research ZHVI** — metro-level home value index, monthly,
  back to 2000. Smoothed and seasonally adjusted.
- **FRED (St. Louis Fed)** — mortgage rates, Case-Shiller, housing
  starts, new/existing home sales, homeownership rate. No API key
  needed when downloading individual series via `fredgraph.csv`.

For active listings, use the `real_estate_listings` skill instead —
this one is for trends and conditions, not property search.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...`.

Subcommands:

- `metro <pattern> [--limit 10]` — search Zillow ZHVI for metros
  whose name contains `<pattern>` (case-insensitive substring).
  Returns `{metro, as_of, zhvi, mom_pct, yoy_pct, five_year_pct}`
  per match. Use a substring like `"Boston"` or `"Boston, MA"` —
  exact match isn't required.
- `rates` — current Freddie Mac PMMS mortgage rates from FRED:
  30-year and 15-year fixed. Returns the latest weekly observation.
- `national` — top-line national indicators in one call: mortgage
  rates, Case-Shiller national, housing starts, existing home sales,
  median sale price, homeownership rate. Each value includes the
  observation date so you know how stale it is.

## Caching

Zillow's ZHVI metro CSV is ~10 MB and slow to download. The script
caches it for 6 hours under your system temp dir
(`<tempdir>/pyagent-housing-market/`). FRED CSVs are tiny and pulled
fresh each call.

## Analyst framing — how to read the numbers

When presenting results, contextualize rather than dumping numbers:

**ZHVI year-over-year change:**
- `>10%` — overheated / bubble territory
- `5–10%` — hot market
- `2–5%` — healthy normal (long-term trend ≈ wage growth + inflation)
- `0–2%` — soft / flat
- `<0%` — declining; look at how broad and how deep

**Mortgage rate context:**
- A 1 pp change in 30y rate ≈ ~10% change in buying power for a
  fixed monthly payment. So 6% → 7% means the same payment buys
  ~10% less house. Mention this when rates have moved meaningfully
  since the user's reference point.
- Long-term US 30y average is ~7–8% nominally; sub-5% is historically
  cheap.

**Case-Shiller vs ZHVI:**
- Case-Shiller is repeat-sales (same home sold twice), so it isolates
  price change on identical properties. Lags by ~2 months.
- ZHVI is hedonic/imputed for *all* homes including ones not for
  sale. More current but not directly comparable methodology.
- For "is the market up or down?" questions, both should agree in
  direction; if they diverge sharply, mention it.

**Inventory / months of supply:**
- `<4 months` — sellers' market
- `4–6` — balanced
- `>6` — buyers' market
- This skill doesn't pull months-of-supply directly; flag it as a
  follow-up the user could research via NAR or local Redfin reports.

**What's missing here:**
- Metro inventory and DOM — Redfin Data Center has these but the
  files are >100 MB; not pulled by default. Mention if asked.
- Rents — Zillow ZORI exists; not pulled by default.
- Sub-metro (county/ZIP) granularity — same source publishes county
  and ZIP files; not wired up. Easy follow-up.

## Typical flows

- "What's happening in the Boston housing market?" →
  `cli.py metro Boston`, then `cli.py rates` for the rate context,
  and frame the YoY using the bands above.
- "Compare Austin and Phoenix" →
  `cli.py metro Austin` and `cli.py metro Phoenix`, then narrate the
  contrast (faster cooling, deeper drop, etc.).
- "Are we in a buyer's or seller's market?" →
  `cli.py national` for the macro shape; note that the answer is
  local and that this tool doesn't have months-of-supply.
