---
name: social_handles
description: Find candidate social handles for a person's name across major networks via public search APIs.
---

# Social handles

Maps a real name to plausible handles on social networks. Pairs with
`social_profile`:

    social_handles search   "Jane Doe"        → likely handles per network
    social_handles candidates "Jane Doe"      → handle string variants
    social_profile  find    janedoe           → confirm + summarize

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `search <name> [--limit 5] [--timeout 5]` — query the public
  user-search APIs in parallel. Returns a JSON list of
  `{site, matches: [{handle, name, url, bio?}]}`, one entry per site.
  Sites with zero matches are still listed (`matches: []`) so you
  can see what was tried.
- `candidates <name>` — pure string generation, no network calls.
  Returns a JSON list of likely handle variants
  (`janedoe`, `jane.doe`, `jdoe`, `doe`, ...). Feed each to
  `social_profile find` to check existence.

## Sites searched by `search`

Public, auth-free search endpoints only:

- GitHub Users
- GitLab Users
- Reddit Users
- Mastodon Accounts (mastodon.social instance)
- Bluesky Actors
- Keybase

Auth-walled platforms (LinkedIn, X/Twitter, Facebook, Instagram) are
not searchable without OAuth — use `social_profile suggest <name>`
for browser-driven leads on those.

## Notes for the agent

- Match quality varies. GitHub and GitLab match on real-name fields,
  so "Jane Doe" surfaces users whose profile name (not handle) is
  Jane Doe. Bluesky and Mastodon match on handle + display name.
  Reddit's user search is fuzzy and noisy; treat the top result as a
  lead, not a confirmation.
- Same name ≠ same person. Cross-reference with bio, location, or
  a known handle on another site before claiming identity.
- `candidates` is dumb string generation — it does not check
  existence. The pipeline is `candidates` → loop through
  `social_profile find` for a positive ID.
- Probes run in parallel with a default 5s timeout each, total
  runtime bounded near 5s.
- All output is JSON on stdout. Failed probes return
  `{site, matches: [], error: "..."}` so one site outage doesn't
  break the run.

## Typical flows

- "Find Linus Torvalds online" →
  `cli.py search "Linus Torvalds"`, then `social_profile find` on
  the most plausible handle to get a full profile summary.
- "Generate handle guesses for Jane Doe" →
  `cli.py candidates "Jane Doe"`, then loop the list through
  `social_profile find` until something hits.
