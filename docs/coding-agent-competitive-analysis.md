# Coding-Agent Competitive Analysis & pyagent Tuning Plan

Status: research-and-recommendation note (May 2026). Not a design
spec — a menu of changes ranked by expected ROI for pyagent's
programming quality. Sources are linked inline; companion issue on
GitHub tracks adoption decisions.

## What this is

A read of the four leading agentic coding harnesses — **Claude
Code**, **OpenAI Codex (CLI + Cloud)**, **xAI Grok Code Fast 1 /
Grok Build**, and **Google Gemini CLI / Antigravity / Jules** —
focused on the *mechanisms* that explain their programming
quality, then mapped to concrete pyagent changes across roles,
plugins, tools, and system-prompt assembly.

The frame is deliberately not "copy what's popular." For every
borrowed pattern, the section names the *failure mode it
prevents*. Patterns without a clear quality mechanism are
omitted.

---

## 1. What each system is doing well, in one paragraph

**Claude Code.** A small narrow toolset (Read/Edit/Write/Grep/Glob/Bash)
where misuse fails loudly. Exact-match `Edit` forces a re-read of
stale files instead of letting silent regressions through. A
two-layer memory split (human-written `CLAUDE.md` + model-written
auto-memory) survives `/compact`. Subagents (Explore, Plan,
general-purpose) run in their own context windows so the main
thread doesn't get polluted with grep dumps. Prompt caching is the
architectural backbone: tools → system → CLAUDE.md → conversation,
in that order, with mid-session tool changes treated as incidents.
Plan Mode is implemented as a *tool-state flag*, not a tool-set
swap, specifically to preserve cache.

**OpenAI Codex.** A very small tool surface — essentially `shell`,
`apply_patch`, `update_plan`, plus MCP. The `apply_patch` envelope
(`*** Begin Patch / @@ / +/-/space / *** End Patch`) is parsed by
the harness, not by `patch(1)`, so malformed diffs fail
structurally. `AGENTS.md` is a hierarchical, version-controlled,
plain-text project memory with explicit precedence
(global → project root → leaf), 32 KiB cap, and concatenation by
proximity. The system prompt enforces preambles (1-sentence ack +
1–2-sentence plan every 1–3 tool calls), a tight Markdown contract
(Title-Case 1–3 word headers, 4–6 bullets, backticks on
identifiers, `path:line` citations, no nesting), and an explicit
"don't fix unrelated bugs" anti-scope-creep clause. Sandbox modes
(`read-only` / `workspace-write` / `danger-full-access`) plus
approval policies remove the "ask before every command" friction
without losing the escape walls.

**Grok Code Fast 1 / Grok Build.** A model trained *to a specific
agentic harness* — RL signal was real PRs executed with grep, file
edit, and shell. xAI's published prompt guide prescribes a
three-part system prompt (role → constraints → tool contracts),
"plan-first / execute-second", surgical context selection over
codebase dumps, and **native function-calling, never XML
emulation** ("XML-tool emulation degrades the model" — direct
quote). Cache hit-rate >90% is the headline performance lever;
cached input is 10× cheaper than fresh input ($0.02 vs $0.20 per
M). Grok Build (the CLI) ships **8 concurrent agents per project +
Arena Mode** that auto-ranks outputs — pass@k as a UI feature.

**Gemini CLI / Antigravity / Jules.** A compositional system-prompt
builder (`renderPreamble`, `renderCoreMandates`, …, `renderUserMemory`)
toggleable by mode. **Inquiry-vs-Directive** default: assume requests
are research-only unless they contain an explicit action verb.
**Strategic Re-evaluation** rule: after 3 failed fix attempts, stop
patch-spamming, restate the task, list assumptions, propose a
different architectural approach. Plan Mode is a hard "no writes"
constraint lifted only by an explicit transition tool. The
Context-Efficiency block is annotated with a comment that it must
not be edited without re-running SWE-bench — the system prompt is
treated as load-bearing code with regression tests. Antigravity
adds an agent-driven Chrome browser for end-to-end UI validation
and "Artifacts" (screenshots, walkthrough recordings) for
structured review.

---

## 2. Cross-cutting mechanisms

The same failure modes show up across all four systems, and the
fixes converge:

| Failure mode | Mechanism that prevents it |
| --- | --- |
| Stale-context regressions (model edits a file it hasn't recently read) | Exact-match `Edit` + read-before-write enforcement (Claude); `apply_patch` envelope with required context lines (Codex) |
| Whole-file context bloat | Line-numbered, paginated `Read` (Claude); `read_file` with `start_line`/`end_line` (Gemini); `grep` with `before`/`after`/`context` so one call replaces grep+read |
| Trigger-happy edits on questions | Plan Mode (Claude/Gemini/Codex); Inquiry-vs-Directive default (Gemini) |
| Patch-spamming on hard bugs | Strategic Re-evaluation after N=3 (Gemini); preambles every 1–3 tool calls (Codex) |
| Wandering tool calls / no narration | Preambles before tool calls (Codex); `update_plan` with one-`in_progress`-at-a-time (Codex) |
| Cache invalidation | Order: tools → system → project doc → conversation (all four); never mutate prefix mid-session; Plan Mode is a state flag, not a tool swap (Claude) |
| Long sessions become incoherent | Sub-agents with isolated context (Claude/Gemini); auto-memory survives compaction (Claude) |
| Hallucinated docs / URLs | Refuse-to-invent-URLs sentence (Claude); refuse-to-fabricate clause (Codex) |
| Output formatting that hurts review | Tight Markdown contract — title-case 1–3 word headers, 4–6 bullets, `path:line` (Codex) |
| "Bias to action" missing | Explicit "bias to action; resolve via tools before asking" (Codex); subagents discouraged for trivial work (Gemini) |
| Scope creep / gold-plating | "Don't fix unrelated bugs; match existing style" (Codex); "Default to writing no comments" (Claude) |

---

## 3. Where pyagent already stands

pyagent has, today (May 2026):

- A **stable/volatile cache split** in `SystemPromptBuilder.build_segments()` —
  matches Anthropic's caching guidance directly. The split *actually
  matters* and is documented in code.
- A **plugin system** with `register_tool`, `register_prompt_section`,
  `register_provider`, plus lifecycle hooks (`on_start`, `on_end`,
  `after_response`, `before_tool`, `after_tool`). Hooks today are
  observers (no controlling return value); the design doc names
  controlling hooks as a v2 surface.
- **Subagents with `ask_parent`/`reply_to_subagent`** — a stronger
  primitive than Claude Code's blocking subagents, since pyagent's
  subagents can converse mid-task with the parent.
- **Memory** as a plugin (`memory-markdown`): `USER.md` auto-loaded
  into the prompt, `MEMORY.md` read on demand. Plugin-owned by
  design.
- **Roles**: `SOFTWARE_ENGINEER`, `RESEARCHER`, `REVIEWER`, `SCRIBE` —
  selected per subagent, with a per-role body appended into the
  prompt.
- **Skills** (`SKILL.md` files) and a write-skill / write-plugin
  authoring path.
- **Tools**: `read_file` (line-numbered, paginated to 2000),
  `edit_file` (exact-string-match, expand-or-`replace_all`),
  `write_file` (with `append=True` chunking), `grep`, `glob`,
  `list_directory`, `execute` (60s timeout), `run_background` /
  `read_output` / `wait_for` / `kill_process`, `fetch_url`,
  `html_select`, `pip_install`.
- **Task tracker** (`add_task`/`update_task`/`list_tasks`) with a
  status footer.
- **Code Mapper** plugin (tree-sitter symbol extraction across 24
  languages) — a pyagent-original feature with no direct equivalent
  in the four reference systems.

What pyagent does *not* have, that the reference systems do:

- A **Plan Mode** (read-only execution mode gated by an explicit
  transition tool).
- An **Inquiry-vs-Directive** default in the system prompt.
- A **Strategic Re-evaluation** rule after N failed attempts.
- **Preambles before tool calls** as a system-prompt convention.
- A **Markdown output contract** in the system prompt (Codex has
  the strictest one).
- A **hierarchical project-doc convention** (`AGENTS.md` /
  `CLAUDE.md` / `GEMINI.md` style), walked from the git root down to
  cwd with concatenation by proximity. pyagent has a single
  plugin-owned `USER.md`; that's not the same shape.
- **Refuse-to-invent-URLs** (and refuse-to-fabricate generally) as
  explicit prompt clauses.
- A **search tool that returns surrounding context** (one call
  replacing grep + follow-up `read_file`).
- **Controlling hooks** (PreToolUse that can block / mutate args).
  v1 docs explicitly defer this.
- A **two-tier model strategy** (heavy planner + cheap implementer)
  as a first-class config, not a per-task improvisation.
- **Best-of-N parallel execution** as a UX (Grok Build's Arena
  Mode).
- A **system-prompt full-replace env var** (`GEMINI_SYSTEM_MD`
  pattern) for power-user / domain-specific tuning.
- **Sandboxing modes with documented escalation** (`read-only` /
  `workspace-write` / `danger-full-access`). pyagent has the
  workspace permission boundary but not modes.

---

## 4. Recommendations, ranked by expected impact

ROI scoring: H = high (likely large quality lift, low implementation
cost), M = medium, L = nice-to-have. Each item names the
*failure mode it prevents* so the value is auditable.

### H1. Add Plan Mode

**Mechanism:** A read-only execution mode entered via
`enter_plan_mode()` and exited via `exit_plan_mode(plan: str)`.
While in plan mode, write tools (`write_file`, `edit_file`,
`execute` for non-read commands, `pip_install`) are disabled at
the tool layer; the model can only read, search, and reason. The
exit tool requires the model to produce a structured plan, which
the user approves before write tools become available again.

**Implementation shape:** A new core flag on the agent's tool
dispatcher (mirrors how Anthropic implements it as a state
variable, *not* a tool-set swap — preserves prompt cache).
`prompts.py` injects a "you are in plan mode" section conditional
on the flag; tools check the flag and return
`<refused: plan mode is active; call exit_plan_mode first>` when
denied. The user-facing transition can be the existing checklist
UI.

**Failure mode prevented:** Trigger-happy edits on questions; "ran
12 commands before realizing I misunderstood."

**Cost:** ~150–250 LoC across `tools.py`, `prompts.py`,
`agent.py`. No prompt-cache penalty if implemented as a flag.

### H2. Inquiry-vs-Directive default in SOUL/PRIMER

**Mechanism:** One paragraph in `SOUL.md` (or `PRIMER.md`) saying
the default for ambiguous requests is research-and-explain, not
modify. The model proceeds to writes only when the request
contains an action verb ("fix", "add", "refactor", "rename")
*or* the user has already approved a plan. Borrows directly from
Gemini CLI's prompt.

**Failure mode prevented:** Over-edits on questions like "why
does X happen?" or "is the auth flow correct?" — currently the
model may dive into edits when explanation was wanted.

**Cost:** ~100 words in `pyagent/defaults/SOUL.md` (or
`PRIMER.md`). Zero code.

### H3. Refuse-to-fabricate + refuse-to-invent-URLs clauses

**Mechanism:** Two sentences in `SOUL.md`:

> Never generate or guess URLs unless they're documentation links
> you've already verified or the user provided them. When you don't
> know a fact (file path, function name, flag, API shape), say so
> and verify with a tool — don't fabricate.

`PRIMER.md` already has the "don't invent" paragraph for paths and
APIs; this strengthens it and adds the URL clause that Claude Code
finds load-bearing.

**Failure mode prevented:** Hallucinated docs URLs in summaries;
hallucinated config flags / function names in suggestions.

**Cost:** ~50 words in `SOUL.md`. Zero code.

### H4. Strategic Re-evaluation after 3 failed attempts

**Mechanism:** A `PRIMER.md` paragraph: *"If three consecutive fix
attempts on the same problem fail, stop. Restate the task in your
own words, list the assumptions you've been making, and propose a
materially different approach before trying again. Patch-spam is a
sign of a wrong model, not a wrong patch."* Optionally enforced by
a `before_tool` hook that counts consecutive `edit_file` /
`execute` failures on the same target and injects a reminder.

**Failure mode prevented:** Long debug-loop tail where the agent
makes 12 small edits trying to fix a test that's failing for a
reason none of those edits address.

**Cost:** ~80 words in `PRIMER.md`; optional plugin
~50 LoC.

### H5. AGENTS.md-style hierarchical project doc

**Mechanism:** A new core feature (or bundled plugin) that walks
from the git root down to cwd, looking for `AGENTS.md` (or a
configurable name list) at each level, concatenating root → leaf
with a 32 KiB total cap. Loaded into the **stable** prompt segment
behind the existing cache breakpoint.

Unlike pyagent's current `USER.md` (single file, plugin-owned),
this is *project-scoped, version-controlled, and shareable*. It is
the pattern that Codex, Claude Code, and Gemini CLI converge on
(`AGENTS.md` is now stewarded by the Linux Foundation; 60k+ repos
use it). Adopting it makes pyagent immediately useful in repos
that already carry one.

**Failure mode prevented:** "Set up the agent for this repo"
becomes a 3-line `AGENTS.md` instead of plugin authoring.

**Cost:** ~150 LoC bundled plugin. The discovery walk and
size-cap logic are mechanical.

### H6. Markdown output contract

**Mechanism:** A short section in `SOUL.md` or `TOOLS.md`
prescribing:

- File citations as `path:line` (clickable in most terminals).
- Backticks for commands, paths, identifiers, env vars.
- Headers Title Case, 1–3 words, only when they add structure.
- Bullets `-` + space, ≤6 per list, ordered by importance, no
  deep nesting.
- No ANSI codes in model output.

Codex's prompt has the clearest version of this; copy it nearly
verbatim, adapt to pyagent's voice.

**Failure mode prevented:** "I have to re-read the agent's reply
twice to find the file it changed." Output formatting is part of
*coding* quality — it's how the user re-enters context after a
long agent run.

**Cost:** ~120 words in a persona file. Zero code.

### H7. `grep` returns surrounding context

**Mechanism:** Add `before: int = 0`, `after: int = 0`, and
`context: int = 0` parameters to `grep` (mirroring `rg
-B/-A/-C`). Result format includes the surrounding lines with
line numbers, formatted so the model rarely needs a follow-up
`read_file`.

**Failure mode prevented:** The double-call pattern (grep → read)
that doubles round-trips on the most common discovery
operation. Measurable token savings on long sessions.

**Cost:** ~40 LoC in `tools.py` plus a doc paragraph in
`TOOLS.md`. Re-uses ripgrep if available, falls back to the
existing implementation.

### H8. Anti-scope-creep clause

**Mechanism:** Add to `PRIMER.md` (Codex-style): *"Don't fix
unrelated bugs you notice while doing the task. Don't refactor
nearby code 'while you're in there'. Match the surrounding style —
don't rewrite a module's conventions to suit your taste. If you
think a separate change is worth making, surface it as a follow-up,
don't bundle it in."*

**Failure mode prevented:** PR-review pain from agent-authored
diffs that touch six files when the task touched two.

**Cost:** ~100 words. Zero code. The current
`SOFTWARE_ENGINEER.md` role hints at this; the clause makes it
universal.

---

### M1. Preamble convention

**Mechanism:** A `PRIMER.md` paragraph asking for a 1-sentence
acknowledgement and 1–2-sentence plan before tool calls, every
1–3 calls (skip for trivial reads). Optionally rendered into the
status footer.

**Failure mode prevented:** Wandering tool calls; users not
knowing when to interrupt.

**Cost:** ~80 words. Zero code unless a footer renderer is added.

### M2. Two-tier model strategy as first-class config

**Mechanism:** Make `[roles.<name>]` accept a `planner_model =
"anthropic/claude-opus-4-7"` distinct from `model =
"anthropic/claude-haiku-4-5"`. The first turn (or first-pass plan)
runs on the planner; subsequent execution runs on the implementer.
Mirrors a workflow that Grok Code Fast 1 users invented by hand
(heavy planner + cheap implementer) and Anthropic encodes via the
Plan subagent.

**Failure mode prevented:** Either over-paying for
straightforward edits or under-thinking on architectural
decisions. Today these are the same setting.

**Cost:** ~80 LoC in `roles.py` and `agent.py`. The provider
abstraction already supports per-call model selection.

### M3. Sandbox modes with documented escalation

**Mechanism:** Three named modes — `read-only`, `workspace-write`
(default), `unrestricted` — selected by CLI flag and surfaced into
the system prompt. `workspace-write` matches today's behavior
(read anywhere in workspace, write only inside workspace,
permission prompt for outside-workspace and shell side effects).
`read-only` blocks `write_file`/`edit_file`/`execute` non-read
commands at the tool layer. `unrestricted` lifts the
permission-prompt requirement (still respects core safety
checks).

**Failure mode prevented:** "I need to ask before every command"
fatigue (currently mitigated by per-tool permission prompts but
not by a documented mode).

**Cost:** ~120 LoC across `permissions.py`, `cli.py`,
`prompts.py`. Probably the right time to also do M3a:

### M3a. `apply_patch`-style structured edit envelope (optional)

A purely *additive* tool, not a replacement for `edit_file`. Takes
a Codex-style envelope; `edit_file` remains the recommended path.
Worth doing only if benchmarks show models making fewer first-pass
diff errors against the envelope than against exact-string Edit —
not certain that's true given pyagent's exact-match Edit is
already strong.

**Cost:** ~150 LoC. Defer until benched.

### M4. Auto-load `USER.md` walks workspace, not config dir

**Mechanism:** Today the bundled `memory-markdown` plugin loads
one `USER.md` at a fixed location. Move to a Codex-style hierarchy:
look in `~/.pyagent/USER.md` (global) → `<git-root>/AGENTS.md` (or
`USER.md` for back-compat) → `./.pyagent/USER.md` (worktree).
Concatenate root → leaf with 32 KiB cap. Survives subdirectory
changes mid-session.

**Failure mode prevented:** "The model knows about my house but
not about this repo" — global vs. project memory should both
work.

**Cost:** ~100 LoC inside the existing memory plugin. Could merge
with H5 (treat `AGENTS.md` and `USER.md` as fallbacks for the
same hierarchy).

### M5. Controlling hooks (v2 of plugin API)

**Mechanism:** Promote `before_tool` from observer to controller —
return values can `block` (with reason), `mutate_args` (to enforce
sandbox / lint rules), or `allow`. Enables the Claude-Code "hooks
for deterministic enforcement" pattern: things the user said
"always X" become hook-enforced rather than memory-suggested.

**Failure mode prevented:** Memory-encoded preferences that the
model silently ignores. Hooks make them deterministic.

**Cost:** ~80 LoC plus a docs/plugin-design v2 update. Backward
compatible with v1 observer hooks (just make the return value
optional).

### M6. `GEMINI_SYSTEM_MD`-style full-replace env var

**Mechanism:** Honor `PYAGENT_SYSTEM_MD=/path/to/file.md`. If set,
the file *replaces* SOUL/TOOLS/PRIMER entirely (plugin sections
still apply). Power-user knob for adversarial debugging or
domain-specific deployments without forking pyagent.

**Failure mode prevented:** "I need to swap the entire prompt for
this experiment" without forking.

**Cost:** ~30 LoC in `prompts.py` / `cli.py`.

### M7. Update `SOFTWARE_ENGINEER` role to encode the H-series

Once H1–H8 land in core/persona files, prune
`SOFTWARE_ENGINEER.md` of anything now-universal. Add an explicit
"validate after change" step (mirrors Gemini's *Plan → Act →
Validate* loop): run the relevant smoke / unit tests; if a smoke
suite exists, run it before and after.

**Cost:** Documentation-only.

---

### L1. Best-of-N parallel agent runs (Grok Build's Arena Mode)

A CLI subcommand that runs the same prompt in N parallel
subagents, displays the diffs side by side, and lets the user
pick. pyagent's existing async-subagent + `wait_for_subagents`
machinery already supports this; the missing piece is a UX. Defer
until there's user demand.

### L2. Codex Cloud / Jules-style background-agent runner

A separate execution surface (long-running container, returns a
PR) sharing the same role / `AGENTS.md` semantics as the local
CLI. Parallel to Codex Cloud's split. Significant scope; out of
the immediate roadmap, but the role/plugin interface should be
designed today so this works as the *same* role, not a fork.

### L3. Tree-sitter `code_map` integration into the system prompt

The `code_mapper` plugin already exists. Consider auto-rendering a
top-level symbol map of the cwd into the *volatile* prompt
segment when the workspace is small enough — gives the model an
"index" without forcing it to grep around. Tradeoff: cache-hostile
for large repos. Worth a bench.

### L4. Web browser tool (Antigravity-style)

End-to-end UI validation via a headless Chrome subagent.
Significant scope; only useful for web-app development. Defer.

---

## 5. Suggested rollout order

1. **Week 1 — prompt-only changes (H2, H3, H6, H8).** Persona
   file edits, no code. Measurable before code lands.
2. **Week 2 — Plan Mode (H1) + grep context (H7).** Both small,
   both visible immediately.
3. **Week 3 — `AGENTS.md` hierarchy (H5) + memory walk (M4).**
   Combine: one plugin owns the hierarchy and serves both
   user-facing memory and project doc.
4. **Week 4 — Strategic Re-evaluation paragraph (H4) and the
   preamble convention (M1).** Both prompt-only follow-ups whose
   value is easier to see once the harder lifts are in.
5. **Quarter 2 — sandbox modes (M3), two-tier models (M2),
   controlling hooks (M5), full-replace env var (M6).** Larger
   surface; easier to bench against a stabilized v1.

After each step, run the existing
`bench/scenarios/pyagent_self_audit.toml` to catch regressions.
The Gemini team's annotation — *"do not edit context-efficiency
without re-running SWE-bench"* — is a cultural pattern worth
adopting: persona files are load-bearing code with regression
tests.

---

## 6. Things deliberately not recommended

- **Massive context-window strategy (Gemini's 1M+).** pyagent is
  provider-agnostic; designing for the largest available window
  punishes the smaller-context providers. The search-then-read
  pattern is the cross-provider sweet spot.
- **XML tool-call emulation as a fallback.** xAI publishes that
  it degrades the model. pyagent already uses native function
  calling; keep it.
- **Mutating the cached prefix mid-session for any reason.** The
  current `(stable, volatile)` split is correct. Resist temptations
  to put turn-counters / timestamps / reordered tool schemas into
  the stable segment; cache miss costs dwarf any benefit.
- **Replacing `edit_file`'s exact-match contract.** It's the
  single biggest quality lever in the toolset. The `apply_patch`
  envelope is *additive at most*, never a replacement.
- **A "load whole repo" tool.** Codex doesn't have one. Claude
  Code doesn't. Gemini CLI doesn't (despite the context window
  to support it). The pattern is grep + targeted reads — cheaper,
  more focused, and harder to derail.

---

## 7. Sources

Claude Code:
- [How Claude Code Builds a System Prompt — Drew Breunig](https://www.dbreunig.com/2026/04/04/how-claude-code-builds-a-system-prompt.html)
- [Lessons from building Claude Code: Prompt caching is everything — Anthropic](https://claude.com/blog/lessons-from-building-claude-code-prompt-caching-is-everything)
- [Claude Code memory docs](https://code.claude.com/docs/en/memory)
- [Claude Code subagents docs](https://code.claude.com/docs/en/sub-agents)
- [Claude Code skills docs](https://code.claude.com/docs/en/skills)
- [Piebald-AI/claude-code-system-prompts (leak)](https://github.com/Piebald-AI/claude-code-system-prompts)
- [asgeirtj/system_prompts_leaks](https://github.com/asgeirtj/system_prompts_leaks)
- [Claude Code Tools deep-dive — thepete.net](https://blog.thepete.net/claude-code-tools/)

OpenAI Codex:
- [openai/codex repo (Rust CLI source)](https://github.com/openai/codex)
- [apply_patch instructions](https://github.com/openai/codex/blob/main/codex-rs/core/prompt_with_apply_patch_instructions.md)
- [base default system prompt](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/prompts/base_instructions/default.md)
- [Codex CLI features](https://developers.openai.com/codex/cli/features)
- [Sandboxing concepts](https://developers.openai.com/codex/concepts/sandboxing)
- [AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md)
- [Codex prompting guide (cookbook)](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)
- [PLANS.md cookbook article](https://developers.openai.com/cookbook/articles/codex_exec_plans)

Grok:
- [Grok Code Fast 1 — xAI announcement](https://x.ai/news/grok-code-fast-1)
- [xAI Prompt Engineering Guide for grok-code-fast-1 — PromptLayer summary](https://blog.promptlayer.com/xais-prompt-engineering-guide-for-grok-code-fast-1/)
- [Grok-code-fast-1 Prompt Guide — CometAPI](https://www.cometapi.com/grok-code-fast-1-prompt-guide/)
- [Grok Build CLI — adwaitx writeup](https://www.adwaitx.com/grok-build-vibe-coding-cli-agent/)
- [xai-org/grok-prompts (official)](https://github.com/xai-org/grok-prompts)

Gemini CLI / Antigravity / Jules:
- [google-gemini/gemini-cli (GitHub)](https://github.com/google-gemini/gemini-cli)
- [packages/core/src/prompts/snippets.ts (system prompt source)](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/prompts/snippets.ts)
- [Tools API docs](https://google-gemini.github.io/gemini-cli/docs/core/tools-api.html)
- [Sandboxing docs](https://google-gemini.github.io/gemini-cli/docs/cli/sandbox.html)
- [GEMINI.md docs](https://geminicli.com/docs/cli/gemini-md/)
- [Plan Mode docs](https://geminicli.com/docs/cli/plan-mode/)
- [Antigravity launch blog](https://developers.googleblog.com/build-with-google-antigravity-our-new-agentic-development-platform/)
- [Jules](https://jules.google/)
- [Context engineering in Gemini CLI — Datta](https://aipositive.substack.com/p/a-look-at-context-engineering-in)

Benchmarks:
- [SWE-Bench 2026: Claude Opus 4.7 vs GPT-5.3 — TokenMix](https://tokenmix.ai/blog/swe-bench-2026-claude-opus-4-7-wins)
- [SWE-Bench Pro Leaderboard — Morph](https://www.morphllm.com/swe-bench-pro)
- [Gemini 3 benchmarks — Vellum](https://www.vellum.ai/blog/google-gemini-3-benchmarks)
- [SWE-Lancer paper](https://arxiv.org/pdf/2502.12115)
