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

`execute` has a hard 60s timeout — fine for git, scripts, builds that
finish in seconds, and one-shot HTTP. For dev servers, file watchers,
long builds, or anything you want to *check on* later, switch to the
background quartet.

- **Start with `run_background(command, name="...")`.** Returns a
  `bg-XXXXXXXX` handle. Pass `name="dev-server"` (or similar) so the
  status reports name something the agent can recognize, not just a
  hex id.
- **`read_output(handle, since=N, max_chars=4000)`** to peek. The
  first read uses `since=0`; subsequent reads pass the previous
  call's `next_since:` value to tail-follow without re-reading bytes
  you've already seen. Stderr (when non-empty) appears under a
  `[stderr]` divider.
- **`wait_for(handle, until="...", timeout_s=...)`** when you need to
  block on a condition before continuing. `until` accepts:
  - `"exit"` — process finished (returns the rc).
  - `"output_contains:STRING"` / `"output_matches:REGEX"` — the
    output mentions a startup banner, a port number, an error, etc.
  - `"silence:Ns"` — N seconds with no new output. Good for "the
    build settled" without picking an exact ready-string.
- **`kill_process(handle)`** to stop a process you started. Idempotent;
  a stale handle returns the standard `<error: handle ... is not
  active>` marker. The agent's normal teardown flushes any leftover
  background processes (SIGTERM with a 2s grace, then SIGKILL), and
  Esc / cancel kills foreground + background together.

Buffers cap at 1MB per stream; overflow drops the oldest 256KB and
prepends `...truncated NN bytes...` on the next read. Don't keep a
chatty `tail -f` running indefinitely if you only need to confirm a
thing started.

## Web pages and HTML

- **Don't write your own HTML scrub.** `fetch_url` saves the raw page
  to a session attachment and returns markdown of the article body
  inline. That's one tool call instead of fetch → grep → re-write a
  regex stripper across multiple turns.
- **Reach for `html_select` when structure matters.** Tables, lists at
  a specific selector, links inside a sidebar — markdown of the whole
  page flattens these. `html_select(path, "table.wikitable tr")`
  preserves rows. The path comes from the attachment `fetch_url`
  saved.
- **Use `format="void"` for triage.** When you're fetching several
  candidate URLs to assess relevance later, `fetch_url(url,
  format="void")` skips the inline markdown so you don't pay for
  previews you won't read. The raw is still saved; come back with
  `html_to_md` / `html_select` on the ones you want.
- **`main_content=False`** when the page *is* a document and the
  boilerplate is part of it (Wikipedia, docs, reference pages). The
  default `True` is right for news / blogs / articles where chrome
  dwarfs the body.

## Tracking multi-step work

For genuine multi-step jobs — three or more distinct subtasks, or
work that spans several tool batches — maintain a checklist with
`add_task` / `update_task` / `list_tasks`. The user sees current
progress in the status footer (`3/7 · "writing migration"`), so the
list is also a status indicator, not just an internal note.

- **Skip the checklist for one-shot work.** Single edit, single
  question, quick lookup → no list. Indiscriminate use is worse
  than none — it adds noise without any of the focus benefit.
- **Plan up front when the shape is clear.** `add_task` for each
  step before starting, then drive each one to `in_progress` →
  `completed` as you go.
- **Exactly one task `in_progress` at a time.** Move the previous
  one to `completed` (or `cancelled`) before starting the next.
  Two "in_progress" at once means you aren't tracking either.
- **Mark `completed` immediately when done — don't batch.** The
  user's footer lags otherwise, and you lose the self-monitoring
  benefit on the next turn.
- **`cancelled` (with a `note`) when you abandon a step.** Don't
  silently leave it `pending`; the user reading the list later
  should be able to tell what happened.
- **Titles are short imperative phrases.** "write migration",
  "run tests", "update README" — they appear in a one-line footer.

## Asking the parent mid-task (subagents only)

Subagents have an `ask_parent` tool that pauses the subagent's
work and sends a question up to the parent agent. The parent
sees the question as a user-role message at the start of its
next turn and answers via `reply_to_subagent(request_id, answer)`.

- **Use sparingly.** Each ask costs the parent a turn cycle
  and blocks your work. Don't ask for things you can answer
  yourself by reading the prompt or running a quick tool call.
- **Be concrete and self-contained.** The parent has its own
  context but doesn't have yours. Include any specifics it
  needs to answer without a follow-up round-trip.
- **One ask at a time.** A second `ask_parent` while the first
  is pending is refused. Wait for the answer.
- **Good fits:** missing dependency the parent should install,
  ambiguous spec where you need a tie-breaker, permission
  question outside your role's scope.
- **Bad fits:** "what should I do next?" (too vague), "is this
  right?" (you should be able to verify), anything answerable
  by reading the system prompt.

For parent agents replying: extract `request_id` from the
`[subagent X asks (req=...)]` bracket of the inbound message
and call `reply_to_subagent(request_id, answer)` exactly once.
Replying twice to the same request fails — the entry is removed
on first reply.

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
