---
name: real_estate_listings
description: Build search URLs for active real estate listings (Zillow / Redfin / Realtor.com / Trulia) and area-research links for a city or ZIP.
---

# Real estate listings

URL constructor for property search. There is **no good free public
API for active listings** — Zillow killed theirs in 2021, Redfin and
Realtor.com never had one, and live MLS data flows through Bridge
Interactive / RESO Web API behind per-MLS partnership agreements.
Paid resellers exist on RapidAPI but coverage and reliability vary.

So this skill takes the pragmatic path: emit clean, filterable URLs
the user (or the agent via a browser) can open. Pair with
`housing_market` for trend context.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...`.

Subcommands:

- `search <city> --state ST [--type for_sale|for_rent|sold] [--beds N] [--baths N] [--price-min N] [--price-max N]` —
  build listing-search URLs for Zillow, Realtor.com, Trulia, plus a
  Redfin Google fallback. Filters are baked into the Zillow URL where
  the path-style filters apply; other sites get the base URL and the
  user can refine in-browser.
- `address <full address>` — build deep-link URLs for a specific
  address: Zillow address search, Redfin address search via Google,
  Realtor.com search, county GIS via Google. Useful for "what's this
  property worth / what records exist".
- `area <city> --state ST` — research links for an area:
  demographics (Census QuickFacts), schools (GreatSchools),
  walkability (WalkScore), market overview (Zillow home values),
  general livability (Niche, BestPlaces, AreaVibes), flood risk
  (FEMA NFHL), crime (AreaVibes). Mix of direct URLs and Google
  fallbacks where slugs are brittle.

State must be a 2-letter abbreviation (e.g. `MA`, `CA`).

## Notes for the agent

- **Output is URLs, not data.** Hand them to the user, or — if you
  have a browser tool — open them. This skill does not scrape.
- Zillow path-style filters are most reliable for `for_sale`. For
  `for_rent` and `sold`, the type segment changes but bed/price
  filters are best-effort; mention that Zillow may collapse extras.
- Address lookups: Zillow's URL routing is messy (it expects an
  internal ID for direct links). The script falls back to a search
  URL that lands the user on the right page after one click.
- For commercial real estate, none of these are right — point the
  user at LoopNet or Crexi instead and don't pretend this covers it.

## Site selection — which platform is best for what

When the user is choosing where to look, surface this rather than
just listing all four:

- **Zillow** — biggest inventory, has Zestimate. Listings can lag
  status changes (a "for sale" home may already be pending). Best
  general starting point.
- **Realtor.com** — pulls directly from MLS, so listing status is
  the most current. Slightly thinner UX but the truth source.
- **Redfin** — agent-curated listings with extra data (price/sqft
  history, walk score baked in). Coverage is best in metro areas
  Redfin operates as a brokerage.
- **Trulia** — owned by Zillow. Better neighborhood-context
  features (crime, commute heatmaps). Use when the user cares about
  *area* fit, not just the property.

## Typical flows

- "3-bed houses in Somerville under $900k" →
  `cli.py search Somerville --state MA --type for_sale --beds 3 --price-max 900000`.
- "Pull info on 123 Main St, Boston, MA 02118" →
  `cli.py address "123 Main St, Boston, MA 02118"`.
- "Tell me about Brookline, MA as a place to live" →
  `cli.py area Brookline --state MA`, then narrate using
  `housing_market metro Boston` for the price context.
