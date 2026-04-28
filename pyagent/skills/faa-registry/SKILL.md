---
name: faa-registry
description: Look up FAA aircraft registry records by tail number, owner name, serial, make/model, or Mode S code (US-registered aircraft only).
---

# FAA aircraft registry

Wraps the public FAA registry inquiry site at `registry.faa.gov`. The
site has no JSON API; the script fetches the HTML pages and emits a
cleaned text rendering of the result tables.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it with
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `lookup <n_number>` — full record for a single US tail number. Pass
  with or without the leading `N`. Prints owner, address, serial,
  make/model, year, airworthiness class, engine, Mode S hex, status,
  or an explicit "no record" string if the tail is unassigned.
- `search-owner <name>` — search by registered owner name. Returns
  matching N-numbers with make/model. The site paginates; the script
  prints only the first page and notes when more pages exist.
- `search-make-model <make> [model]` — search by manufacturer and
  optional model. Same pagination caveat.

Quote arguments that contain spaces (`"ACME AVIATION LLC"`).

## What this is good for

- "Who owns N12345?" → `cli.py lookup N12345`.
- "Find all Cessna 172s registered to Acme Aviation" — combine
  `search-owner` and filter, or `search-make-model` and
  cross-reference. Don't expect to do bulk pulls; the registry is
  rate-limited and each call is one HTTP GET.
- Pairing with the flight-tracker skill: a Mode S hex (`icao24`) from
  ADS-B can be reversed to a tail/owner via `lookup` (search by
  Mode S Code on the FAA site happens automatically when the input
  looks like hex — but in practice prefer the N-number lookup).

## What it isn't

- Not a worldwide registry. US-registered (N-numbers) only. For
  foreign registrations the lookup will return "no record".
- Not real-time. Records reflect what the FAA has on file; new
  registrations and deregistrations may lag by weeks.
- Not for bulk export. The FAA publishes a downloadable database for
  that — point the user there if they want everything.
