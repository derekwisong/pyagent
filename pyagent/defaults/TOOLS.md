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
- **Discovering files by name → `glob`, not `find` via `execute`.**
  `glob("**/*.py")` returns the same list `find` would, sorted, with
  `.git` / `__pycache__` / `node_modules` already excluded and a hard
  result cap. Pass a list (`["**/*.py", "**/*.pyi"]`) when you want
  multiple extensions in one call.
- **Parallelize when calls are independent.** Two `read_file`s on
  different paths can go in the same turn.
- **Don't dump large outputs.** Logs, test runs, and binary blobs eat
  the context window. Slice with `grep`, ranges, or `head`/`tail`
  via `execute`.

## Editing files

- **Small change → `edit_file`**, not a full `write_file`. Only the
  diff enters the conversation. `old_string` must match once;
  expand it with context if not, or pass `replace_all=True`.
- **Huge write → chunk with `write_file(append=True)`.** Don't
  emit a shell heredoc via `execute` — that wedges the whole file
  into the conversation forever. `append=True` creates the file
  if missing, so the first chunk can use it too.

## Long-running shell

`execute` has a hard 60s timeout. For dev servers, watchers, long
builds, or anything you want to *check on* later, switch to the
background quartet:

- **`run_background(command, name="...")`** — returns a
  `bg-XXXXXXXX` handle. Pass `name="dev-server"` so status reports
  use something readable.
- **`read_output(handle, since=N)`** — tail-follow via the previous
  call's `next_since:` value; first read uses `since=0`.
- **`wait_for(handle, until="...")`** — block on a condition (see
  the tool schema for the `until` grammar).
- **`kill_process(handle)`** — stop. Idempotent; agent teardown
  also flushes leftover background processes.

Don't keep a chatty `tail -f` running if you only need to confirm a
thing started. Buffers cap at 1MB per stream; overflow truncates.

## Web pages

`fetch_url` saves the raw page and returns markdown of the article
body inline — one call instead of fetch-then-strip across turns. Use
`format="void"` to triage many URLs cheaply (raw still saved; come
back with `read_file` / `grep` on the ones worth a look). Set
`main_content=False` for reference pages (Wikipedia, docs) where the
whole document is the content.

## Tracking multi-step work

For 3+ distinct subtasks or work that spans several tool batches,
maintain a checklist with `add_task` / `update_task` / `list_tasks`.
The user sees progress in the status footer (`3/7 · "writing
migration"`).

- **Skip for one-shot work.** Single edit / question / lookup → no
  list. Indiscriminate use is worse than none.
- **Exactly one task `in_progress` at a time** — move previous to
  `completed` (or `cancelled` with a `note`) before the next.
- **Mark `completed` immediately, don't batch** — the footer lags
  otherwise.
- **Titles: short imperative phrases** ("write migration", "run
  tests"). They appear in a one-line footer.

## Asking the parent mid-task (subagents only)

`ask_parent` pauses your work and sends a question up. The parent
replies via `reply_to_subagent(request_id, answer)` (request_id
comes from the `[subagent X asks (req=...)]` bracket they receive).

- **Use sparingly** — each ask blocks your work and costs a
  parent turn. Don't ask things you can answer yourself with a
  quick tool call or by re-reading the prompt.
- **Be concrete and self-contained** — the parent doesn't have
  your context.
- **One ask at a time.** A second is refused while the first is
  pending.
- **Good fits:** missing dependency, ambiguous spec needing a
  tie-breaker, permission outside your role's scope.
- **Bad fits:** "what should I do next?" (too vague), "is this
  right?" (verify it yourself).

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
