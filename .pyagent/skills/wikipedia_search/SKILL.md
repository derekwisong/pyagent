---
name: wikipedia_search
description: Search Wikipedia and fetch article summaries or extracts.
---

# Wikipedia search

Hits the public English Wikipedia API (`en.wikipedia.org/w/api.php`
and the REST summary endpoint). No key required, but Wikimedia asks
for a descriptive User-Agent — the script sets one.

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `search <query> [--limit 5]` — full-text search; returns a JSON
  list of `{title, snippet, pageid}` (snippet has HTML stripped).
  Use this first when the user's phrasing isn't an exact title.
- `summary <title>` — fetch the REST summary for a known title:
  `{title, description, extract, url, thumbnail}`. The `extract` is
  the intro paragraph in plain text. Title matching is
  case-sensitive after the first letter and follows redirects.
- `extract <title> [--full] [--sentences N]` — plain-text article
  body via `prop=extracts`. Default is the intro section (matches
  what `summary` returns but works on disambiguation/list pages
  where REST summary is unhelpful). `--full` returns the whole
  article; `--sentences N` truncates to the first N sentences.

## Notes for the agent

- Prefer `summary` for "who/what is X" questions on a clear subject
  — one round trip, includes a short description and canonical URL.
- If `summary` returns `<not found>` or a disambiguation marker, run
  `search` first, pick the best title, then `summary` that title.
- Titles use underscores or spaces interchangeably in URLs; the
  script accepts either. Capitalize proper nouns.
- Snippets in `search` results contain `<span class="searchmatch">`
  in the raw API; the script strips those tags before printing.
- All output is JSON on stdout. Predictable failures (no results,
  page not found, rate limit) exit 0 with a `<...>` marker line —
  parse stdout, not the exit code.
- Wikipedia content is CC BY-SA. When quoting more than a sentence
  or two verbatim, cite the article URL the script returns.

## Typical flows

- "Who is Ada Lovelace?" →
  `cli.py summary "Ada Lovelace"`.
- "Find articles about the Voyager probes" →
  `cli.py search "Voyager probe" --limit 5`, then `summary` the
  most relevant title.
- "Give me the full article on the Rosetta Stone" →
  `cli.py extract "Rosetta Stone" --full`.
- "First three sentences of the Mars article" →
  `cli.py extract Mars --sentences 3`.
