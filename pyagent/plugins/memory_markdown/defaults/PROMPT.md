## Memory tools

USER and MEMORY live under the plugin's data dir. Use these tools
rather than generic file ops — they know where the files live and
keep the index consistent.

- **`add_memory(category, title, content, filename="", hook="", force_new_category=False)`**
  Save a new memory in one call: writes the body file under
  `memories/<filename>` (with a `created_at` frontmatter that read
  tools strip on the way out) and inserts a bullet under
  `## <category>` in MEMORY.md. Empty `filename` is derived from
  `title` (`"Stack choices"` → `stack_choices.md`). Drift guard
  refuses close-but-not-equal new categories; pass
  `force_new_category=True` to override.
- **`read_memory(file)`** — fetch a body. The catalog and USER are
  auto-loaded into your prompt; this tool is for the bodies.
- **`write_memory(file, content)`** — overwrite a body. Pass
  `file=""` to overwrite MEMORY.md (the catalog itself) — rare,
  for consolidation.
- **`write_user(content)`** — overwrite the USER ledger.
- **`update_memory_hook(filename, new_hook)`** — change just the
  hook portion of one bullet in MEMORY.md, without re-emitting the
  whole index. Reach for this when a hook is failing recall —
  generic phrasing, missing distinctive tokens.
- **`recall_memory(query, k=5, min_score=0.0, category=None)`** —
  semantic search across hooks and bodies, when available. Use when
  scanning the catalog isn't enough.

### Categories
Before picking a `category`, scan the `## <heading>` lines already
in MEMORY.md (visible in your prompt; when 5+ headings exist a
one-line *Categories in use:* summary is rendered up top). Use the
closest existing heading rather than spawning a near-duplicate;
`add_memory` will refuse a close-but-not-equal new category and
point at the existing one. Common shapes that recur across users:
**Architecture** (system shape, deployment, service boundaries),
**Database** (schema, migrations, query notes), **Style** (code
conventions, formatting, idioms), **Gotchas** (non-obvious failure
modes worth remembering), **Decisions** (the *why* behind a choice,
especially the ones that surprised future-you), **References**
(links, dashboards, channels). New categories are fine when nothing
fits — but ask "is this really not Decisions / Gotchas?" first.

### Filenames
Lowercase snake_case ASCII with the `.md` suffix
(`stack_choices.md`, `client_naming_convention.md`,
`incident_2026_04_22_payment_pool.md`) — `add_memory` rejects
anything else. Compound names age better than bare topics; the
filename's tokens feed `recall_memory` alongside title and hook,
so descriptive filenames pull their weight at recall time.

### Hooks
The hook is the line beside the link in the index — what future-you
(or `recall_memory`) reads to decide whether to fetch the body. A
good hook:
- Names the *problem* the memory solves, not the solution. "Why
  we picked uv over poetry" beats "Notes on uv".
- Uses words future-you would search for. Distinctive tokens
  (`uv`, `pgbouncer`, `429-from-Algolia`) drive recall when general
  phrasing misses.
- Stays under ~120 chars. Pointers in an index that scrolls on the
  prompt should be a glance, not a read.

Empty hook is allowed but it costs you — recall has only the title
and filename to work with.
