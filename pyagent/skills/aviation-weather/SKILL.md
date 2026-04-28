---
name: aviation-weather
description: Get METARs, TAFs, PIREPs, AFD, AIRMETs/SIGMETs, and station info around an airport identifier (aviationweather.gov, no key required).
---

# Aviation weather

Pulls weather around an airport from `aviationweather.gov/api/data/`
(US National Weather Service). No API key required, but the NWS asks
that you set a User-Agent — the script does.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `brief <airport> [--radius-nm 50] [--hours 2] [--pirep-age 2]` —
  one-call wrapper that returns station info, METARs, TAFs, PIREPs,
  AFD, and advisories as a single JSON blob. Use this first; reach
  for the individual subcommands only when you need to drill in.
- `metars <airport> [--radius-nm 50] [--hours 2]` — current
  observations at the airport plus stations within `--radius-nm`.
- `tafs <airport> [--radius-nm 50]` — terminal forecasts at the
  airport plus nearby airports that issue TAFs.
- `pireps <airport> [--radius-nm 200] [--age 2]` — pilot reports
  anchored on the airport. PIREPs are terse and abbreviation-heavy;
  translate them for the user (e.g. `MOD CHOP 250` → "moderate chop
  at FL250"). PIREPs are gold for pilots — never hide them.
- `afd <airport>` — Area Forecast Discussion (the prose narrative
  from the responsible NWS WFO). Includes the aviation section.
- `advisories <airport> [--radius-nm 200]` — AIRMETs, SIGMETs, and
  CWAs intersecting the airport's neighborhood.
- `station-info <airport>` — lat/lon, elevation, site type
  (METAR / TAF / etc.) for one airport.

## Notes for the agent

- PIREPs are the most pilot-useful and the hardest to read. Don't
  drop them. Translate inline turbulence/icing/cloud abbreviations
  the first time they appear in a session.
- METARs come back with `altim` in **hPa**, not inHg, despite the raw
  text using inHg. If you mention a setting, use the value parsed
  from the `rawOb` text or convert hPa × 0.02953.
- Empty advisories / 204 responses are common — "no current SIGMETs
  in this area" is a legitimate good-news answer.
- `--radius-nm` is converted to a bounding box client-side. The
  underlying API has no global radius parameter except for PIREPs.
- Cite the airport's local time, not just UTC, when the user is at
  that airport — easier to read.

## Typical flows

- "What's the weather at KORD?" →
  `cli.py brief KORD`.
- "Any pireps around Boston this morning?" →
  `cli.py pireps KBOS --radius-nm 150 --age 4`.
- "Will VFR hold for a 1500Z departure from KSFO?" →
  `cli.py tafs KSFO`, `cli.py advisories KSFO`, `cli.py afd KSFO`.
