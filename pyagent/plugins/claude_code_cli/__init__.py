"""claude-code-cli — bundled plugin: pipe a prompt into `claude -p`.

Hands off a self-contained piece of work to Anthropic's Claude Code
CLI in non-interactive mode (`-p --bare`) and returns whatever it
prints. Intended for tasks the agent would rather not pollute its own
conversation with — log triage, scripted refactors, multi-step plans.

The plugin's `[requires] binaries = ["claude"]` gate means the load
step silently skips this plugin (info-level "skipped" log) on machines
without `claude` installed; the tool is never advertised, so the
model can't try to call it.

Tool surface:

  prompt              — required instruction.
  session_name        — friendly label; first call creates a Claude
                        Code session, subsequent calls resume it
                        (process-local mapping).
  context_file        — file piped to claude on stdin (the canonical
                        `cat foo | claude -p "..."` pattern).
  append_system_prompt — extra system-prompt text layered on top of
                        claude's default.
  allow_tools         — list of tool names claude may use; defaults
                        to a read-only safe set so the spawned claude
                        can't bypass pyagent's permission boundaries
                        with Bash/Edit/Write of its own.
  output_format       — "text" (default, human-readable) or "json"
                        (claude's `--output-format json` envelope —
                        useful for chaining when the calling agent
                        wants structured fields like `result`,
                        `total_cost_usd`, etc.).
  json_schema         — optional JSON Schema dict; passed through to
                        `--json-schema` for schema-validated output.
                        Only meaningful when `output_format="json"`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Cap so a hung claude subprocess can't wedge an agent tool-call slot
# indefinitely. 5 min is generous for a single -p turn; raise if real
# workloads need it.
_TIMEOUT_S = 300

# Reject context files larger than this. Protects against accidentally
# piping a multi-GB log through the LLM. 1 MiB is roughly the most a
# 200K-token model can usefully chew in one turn.
_MAX_CONTEXT_BYTES = 1 * 1024 * 1024

# Default allow-list for the spawned claude. Read-only by design:
# pyagent already has its own Bash/Edit/Write that go through the
# permission system; letting a forked claude run those out-of-band
# is a quiet way to bypass that boundary, so the default forces the
# spawned instance into a *reasoning* role. Callers who genuinely
# want write access pass their own list to `allow_tools`.
_SAFE_DEFAULT_ALLOWED_TOOLS = (
    "Read",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
)

_VALID_OUTPUT_FORMATS = ("text", "json")

# Process-local session_name → Claude Code session UUID.
# - First call with a name: allocate UUID4, use `--session-id`.
# - Subsequent calls: use `--resume` against that UUID.
# UUIDs are random per pyagent process; a restart yields fresh
# sessions for the same name (intentional: scoping = process lifetime).
_session_ids: dict[str, str] = {}


def register(api):
    def claude_code_cli(
        prompt: str,
        session_name: str | None = None,
        context_file: str | None = None,
        append_system_prompt: str | None = None,
        allow_tools: list[str] | None = None,
        output_format: str = "text",
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        """Run a prompt through the Claude Code CLI (`claude -p --bare`).

        Reach for this when you want a *separate* Claude instance to
        do a self-contained piece of work — analyzing a log dump,
        running a scripted refactor, generating boilerplate — without
        loading the artifact into your own conversation.

        Pair `context_file` with a focused `prompt` for the
        `cat file | claude -p "..."` pattern: the file streams in on
        stdin, the prompt frames what to do with it.

        Use `session_name` to thread several calls together. The first
        call with a given name creates a fresh Claude Code session;
        subsequent calls resume it, so claude carries context across
        steps (e.g. "refactor X" → "now write a test for that").

        Use `output_format="json"` when you intend to *programmatically*
        consume the response — claude returns a JSON envelope with
        `result`, `session_id`, `total_cost_usd`, etc. Combine with
        `json_schema` to constrain the shape of `result` itself.

        Args:
            prompt: The instruction for claude. Required, non-empty.
            session_name: Optional label. Repeated names share a
                Claude Code conversation for the lifetime of this
                pyagent process.
            context_file: Optional path to a file whose contents are
                piped to claude on stdin. Capped at 1 MiB; trim
                upstream if larger.
            append_system_prompt: Optional text appended to claude's
                default system prompt. Useful for narrowing focus
                ("you are a log triage assistant; respond in bullets").
            allow_tools: Optional list of Claude tool names the
                spawned instance may invoke. Defaults to a read-only
                safe set (Read/Glob/Grep/WebFetch/WebSearch). Pass an
                explicit list to grant more — e.g.
                ["Read", "Edit", "Bash(git *)"] — but consider that
                anything mutating bypasses pyagent's permission
                system.
            output_format: "text" (default) or "json". JSON mode wraps
                the assistant's reply in a structured envelope.
            json_schema: Optional JSON Schema dict; passed to
                `--json-schema` to validate the assistant's `result`.
                Ignored unless `output_format="json"`.

        Returns:
            `session: <name>\\n\\n<claude output>` — the session label
            (or `<one-off>`) plus claude's stdout. In JSON mode the
            output portion is claude's JSON envelope; the caller can
            split on the first blank line and parse the rest.
            Errors come back inline as `<claude error: ...>` /
            `<claude timed out after Ns>`.
        """
        if not prompt or not prompt.strip():
            return "<prompt is empty>"
        if output_format not in _VALID_OUTPUT_FORMATS:
            return (
                f"<claude error: output_format must be one of "
                f"{list(_VALID_OUTPUT_FORMATS)}, got {output_format!r}>"
            )

        cmd = ["claude", "-p", "--bare"]

        if session_name:
            existing = _session_ids.get(session_name)
            if existing is None:
                new_id = str(uuid.uuid4())
                _session_ids[session_name] = new_id
                cmd += ["--session-id", new_id]
            else:
                cmd += ["--resume", existing]

        if append_system_prompt:
            cmd += ["--append-system-prompt", append_system_prompt]

        # `allow_tools is None` → safe defaults; an explicit empty list
        # means "no tools" and we still pass the flag (with no values)
        # so claude doesn't fall back to its full default set.
        tool_list = (
            list(_SAFE_DEFAULT_ALLOWED_TOOLS)
            if allow_tools is None
            else list(allow_tools)
        )
        cmd += ["--allowedTools", *tool_list]

        if output_format == "json":
            cmd += ["--output-format", "json"]
            if json_schema is not None:
                try:
                    cmd += ["--json-schema", json.dumps(json_schema)]
                except (TypeError, ValueError) as e:
                    return (
                        f"<claude error: json_schema is not "
                        f"JSON-serializable: {e}>"
                    )

        cmd.append(prompt)

        stdin_data: str | None = None
        if context_file:
            ctx_path = Path(context_file)
            try:
                # Read one byte over the cap so we can detect overflow
                # without slurping a multi-GB file into memory.
                with ctx_path.open(
                    "r", encoding="utf-8", errors="replace"
                ) as f:
                    stdin_data = f.read(_MAX_CONTEXT_BYTES + 1)
            except OSError as e:
                return (
                    f"<claude error: cannot read context_file "
                    f"{context_file!r}: {e}>"
                )
            if len(stdin_data) > _MAX_CONTEXT_BYTES:
                return (
                    f"<claude error: context_file exceeds "
                    f"{_MAX_CONTEXT_BYTES} bytes; trim upstream>"
                )

        try:
            proc = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"<claude timed out after {_TIMEOUT_S}s>"
        except FileNotFoundError:
            # The [requires] gate should make this unreachable, but
            # PATH can change mid-session — surface a clean error
            # rather than a stack trace.
            return "<claude error: 'claude' not found on PATH>"

        if proc.returncode != 0:
            err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
            return f"<claude error: {err}>"

        label = session_name or "<one-off>"
        out = proc.stdout or ""
        return f"session: {label}\n\n{out}"

    api.register_tool("claude_code_cli", claude_code_cli)
