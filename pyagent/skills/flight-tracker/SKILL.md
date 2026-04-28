---
name: flight-tracker
description: Look up live aircraft state vectors near a point or by ICAO24 hex code via OpenSky Network. Anonymous works; OAuth2 client credentials unlock more.
---

# Flight tracker (OpenSky)

Live ADS-B / Mode S data from the OpenSky Network. The public API
works without authentication, but anonymous callers get fewer credits
and lose access to historical state-vector queries. The script falls
back gracefully to anonymous if no credentials are set.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `states-near <airport_or_latlon> [--radius-nm 50]` — current state
  vectors (lat, lon, altitude, velocity, heading, vertical rate)
  inside a radius. The first argument can be an ICAO airport ID
  (`KORD`) or a `lat,lon` string (`41.96,-87.93`).
- `states-bbox <lamin> <lomin> <lamax> <lomax>` — same data, with
  an explicit bounding box. Use when the user already gave you one.
- `aircraft <icao24> [--hours-back 24]` — last-known state plus
  recent flights for a single aircraft, by 24-bit ICAO hex
  (`ac82ec`). This is the hex you cross-reference against the FAA
  registry's "Mode S Code (Base 16 / Hex)" field.
- `setup-credentials` — interactive walk-through that tells the user
  how to create OpenSky OAuth2 client credentials and where to put
  them. Call this when the user hits a rate limit, asks for history,
  or asks "how do I get more out of this".

## Onboarding the user to credentials

OpenSky moved to OAuth2 client credentials in 2025. Anonymous still
works for the simple "what's flying near me right now" question, but
encourage the user to set up credentials when:

- They want to look up aircraft history (the `/flights/aircraft`
  endpoint requires auth).
- They hit a 429 or "credit-limit" response.
- They're going to use the skill more than a couple of times.

`setup-credentials` prints concrete instructions — register at
https://opensky-network.org/, then export `OPENSKY_CLIENT_ID` and
`OPENSKY_CLIENT_SECRET` in their shell rc. Once those env vars are
present, the other subcommands authenticate automatically.

## Notes for the agent

- A state vector's `icao24` is a 24-bit hex string (e.g. `ac82ec`);
  callsigns can be missing or padded with spaces.
- "Velocity" in OpenSky is meters/second over ground; `geo_altitude`
  and `baro_altitude` are meters. Convert: m/s → kt × 1.94384;
  m → ft × 3.28084.
- `on_ground=True` aircraft still appear — useful to know they're at
  the airport but excluded if the user wants in-air only.
- A bounding box that's too large is rejected; keep `--radius-nm`
  reasonable (≤ 250 nm) for anonymous use.
- This data is best-effort. ADS-B coverage is patchy in remote areas,
  and military aircraft routinely don't broadcast.
