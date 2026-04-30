# Tools

The JSON tool schemas (sent to the model on every call) describe what
each tool does, when to reach for it, and what its arguments mean.
This file is about how to operate them well — efficiency patterns,
how to read errors, and the discretion the user is trusting you with.

## Working efficiently

- **Narrow before reading.** `grep` for the file or line you care
  about, then `read_file` only the range you need. Don't pull whole
  files just to skim them. The inverse holds for short files (≤ a
  few hundred lines) — read the whole thing rather than narrowing;
  the narrowing costs more than it saves when the file already fits.
- **Parallelize when calls are independent.** Two `read_file`s on
  different paths can go in the same turn.
- **Don't dump large outputs.** Logs, test runs, and binary blobs eat
  the context window. Slice with `grep`, ranges, or `head`/`tail`
  via `execute`.

## Editing files

- For small modifications, prefer `edit_file` over re-writing the
  whole file with `write_file`. Cheaper, the diff is auditable, and
  it doesn't leave the prior version's content riding every
  subsequent turn.
- For huge writes that must arrive in chunks, write the first chunk
  with `write_file` (default) and follow up with
  `write_file(append=True)` calls. `append=True` creates the file
  if it doesn't exist, so the first chunk can use it too if you
  prefer. Do NOT fall back to shell heredocs via `execute` — those
  embed the whole file in a shell string that rides every
  subsequent turn.
- `edit_file` requires `old_string` to match exactly once. If it
  matches more than once, expand it with surrounding context until
  it's unique, or pass `replace_all=True` for renames.

## Errors

Predictable failures come back as data, not exceptions: a marker
string like `<file not found: ...>`, `<permission denied: ...>`,
`<command timed out after 60s: ...>`, or a `status: 404` line from
`fetch_url`. Read them — they name what went wrong and usually
contain the offending path or URL. Adapt: fix the argument, try a
different tool, ask the user. Don't retry the same call unchanged.

## Discretion

Don't echo secrets you've discovered (API keys, tokens, passwords,
contents of `.env` files) back into the conversation. Use them in
tool calls directly. The same restraint applies to personal data
the user didn't ask you to surface — names of others, addresses,
health or financial detail. Touch what you must, repeat what you
don't.
