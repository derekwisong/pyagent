---
name: books_search
description: Search Open Library for books by query, ISBN, or work key.
---

# Books search

Hits Open Library (`openlibrary.org`). Public API, no key required.
Strong on older and public-domain titles; weaker on brand-new releases
(for those, the user may want Google Books instead).

## Tool

A single CLI ships with this skill: `scripts/cli.py`. Invoke it as
`python <skill_dir>/scripts/cli.py <subcommand> ...` (the `<skill_dir>`
is the absolute path printed in the header above).

Subcommands:

- `search <query> [--limit 5]` — full-text search across title,
  author, and subject. Returns a JSON list of
  `{title, authors, first_publish_year, work_key, edition_count, isbn, cover_url}`.
  The `work_key` (e.g. `/works/OL27448W`) is what `work` consumes.
  Use this for "books about X" or "what did Y write".
- `isbn <isbn>` — exact edition lookup by ISBN-10 or ISBN-13.
  Returns `{title, authors, publishers, publish_date, pages, url, subjects}`.
  Best when the user gives a specific ISBN or wants format/publisher
  detail that search results don't carry.
- `work <key>` — fetch a work's long-form description and subjects.
  `key` is the path returned by `search` (e.g. `/works/OL27448W`),
  with or without the leading slash. Use this when the user wants a
  synopsis after picking a search result.

## Notes for the agent

- All output is JSON on stdout. Predictable failures (no results,
  not found, rate limit) exit 0 with a `<...>` marker line — parse
  stdout, not the exit code.
- Search results often include an `isbn` array with many editions;
  the script returns the first one. If the user wants a specific
  edition, fall back to `isbn <ISBN>`.
- Cover URLs use the `covers.openlibrary.org` CDN at size `M`. They
  may 404 if no cover is on file.
- Open Library descriptions are plain text or `{type, value}` blobs;
  the script normalises both to a string.
- When the user wants a synopsis, prefer `search` → pick the best
  result → `work <key>`. The two-step is cheap and the work record
  carries the description; search docs do not.
