---
name: social_profile
description: Summarize an individual's public social presence by username, or build search URLs for a name-based deeper search.
---

# Social profile

Quick recon of an individual's public footprint. Hits only auth-free
public APIs and returns a one-line summary per site — name, bio,
canonical URL — not timeline content. Use as a starting point before
the user/agent drills into specific platforms.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `find <username> [--timeout 5]` — check the username across the
  public-API set in parallel. Returns a JSON list of
  `{site, found, url, name?, bio?}`, found-true entries first.
  The URL is filled in even when `found` is false so the user has
  somewhere to click manually.
- `suggest <name>` — given a person's name (in quotes if multi-word),
  return a JSON list of `{site, url}` search-URL templates for
  platforms that need authenticated browsing or name-based search
  (LinkedIn, X/Twitter, Bluesky, Mastodon, Facebook, Instagram,
  Reddit, GitHub, plus a plain Google query). The user follows
  these in a browser; this skill does not scrape them.

## Sites checked by `find`

Auth-free, public-API sites only:

- GitHub, GitLab — code hosting
- Reddit — discussion
- Hacker News, Lobsters — tech discussion
- Mastodon (mastodon.social instance) — fediverse default
- Bluesky (`<username>.bsky.social` handle) — fediverse
- Keybase — identity proofs

Auth-walled platforms (LinkedIn, X/Twitter, Facebook, Instagram,
TikTok, etc.) are deliberately not probed — their public endpoints
are unreliable without OAuth and the data quality is poor. They
appear in `suggest` instead so the user can open a browser session.

## Notes for the agent

- All output is JSON on stdout. Predictable failures (network error,
  unknown shape) exit 0 with the site marked `found: false` and a
  fallback URL — never crash the whole run because one probe failed.
- A `found: true` result is strong signal that *someone* claimed the
  username. It does not prove identity — the same handle on different
  sites may be different people. Surface that uncertainty when
  presenting results.
- The Bluesky probe assumes the `*.bsky.social` handle convention.
  Custom-domain handles (e.g. `derek.example.com`) won't be found
  this way; suggest the user search Bluesky directly via `suggest`.
- Mastodon is checked only on `mastodon.social`. The user may have
  an account on a different instance; mention this when results are
  thin.
- Probes run in parallel with a default 5s timeout each, so total
  runtime is bounded near 5s even if some sites are slow.

## Typical flows

- "Where is gvanrossum online?" →
  `cli.py find gvanrossum`.
- "Find Guido van Rossum's profiles" →
  `cli.py suggest "Guido van Rossum"` first, then drill in with
  `cli.py find <username>` once you know a handle.
