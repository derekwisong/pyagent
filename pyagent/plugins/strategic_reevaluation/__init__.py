"""strategic-reevaluation — bundled v2 demo plugin.

Watches `edit_file` tool results. When the same path fails three
times in a row (consecutive failures, not cumulative across the
session), the plugin injects an H4-flavored "step back and
reconsider" note onto the next assistant turn via the v2
``AfterToolHookResult.extra_user_message`` channel.

The counter is **path-keyed**, not global — a flurry of failures
across different files (a refactor where the same `old_string` is
hunted across N files, two of which legitimately don't have the
text) does not trip the heuristic. Three real-honest-to-goodness
failures on the same file does.

Counter resets on:
  - Any successful `edit_file` call against that path.
  - Any non-`edit_file` tool call against that path (read_file,
    write_file, grep — any of these are evidence the agent is
    actually inspecting the situation rather than blind-retrying).

This plugin is the canonical worked example for the "controlling
hooks" feature: ~80 lines, zero tools registered, demonstrates that
plugin-injected mid-turn feedback is now a 30-line plugin instead of
a paragraph baked into PRIMER.
"""

from __future__ import annotations

from typing import Any

from pyagent.plugins import AfterToolHookResult

CONSECUTIVE_FAILURE_THRESHOLD = 3

# Per-process state. The plugin is `in_subagents = false`, so this
# only ever lives in the root agent's process.
_consecutive_fails: dict[str, int] = {}

# A tool result counts as a failure if the tool returned an error
# marker. pyagent's `<...>` convention covers most cases:
# `<file not found: ...>`, `<error: old_string ... not found>`, etc.
# We also flag `_denied(path)` results (which start with
# `<permission denied to ...>`) as failures.
_FAILURE_PREFIXES = ("<error", "<file not found", "<permission denied",
                     "<is a directory", "<cannot decode", "<no match",
                     "<old_string", "<the original", "<")


RECONSIDER_NOTE = (
    "You've now had several consecutive edit_file failures on the "
    "same path. Step back. Re-read the file from scratch. The "
    "old_string you keep trying to match probably isn't there in the "
    "form you think it is — confirm with read_file or grep before "
    "the next edit, and consider whether the change you're trying to "
    "make is the right one at all."
)


def _is_failure(result: Any) -> bool:
    """edit_file returns a string; failures start with `<...>` markers
    (the codebase's `tools.py` `<...>` convention). A confirmation
    starts with `Wrote ...` or `Replaced ...`."""
    if not isinstance(result, str):
        return False
    s = result.lstrip()
    if not s:
        return False
    # Cheap test: confirmations start with Wrote/Replaced/etc., not
    # with `<`. Anything starting with `<` is a marker per the
    # tools.py convention.
    return s.startswith("<")


def _path_from_args(args: dict) -> str | None:
    if not isinstance(args, dict):
        return None
    p = args.get("path")
    return p if isinstance(p, str) and p else None


def _on_after_tool(name: str, args: dict, result: Any) -> AfterToolHookResult | None:
    path = _path_from_args(args)
    if path is None:
        return None
    if name != "edit_file":
        # Reset the counter on any other tool against this path —
        # evidence the agent is inspecting rather than blind-
        # retrying.
        _consecutive_fails.pop(path, None)
        return None

    if not _is_failure(result):
        # Successful edit. Reset.
        _consecutive_fails.pop(path, None)
        return None

    # Bumping the counter.
    n = _consecutive_fails.get(path, 0) + 1
    _consecutive_fails[path] = n
    if n < CONSECUTIVE_FAILURE_THRESHOLD:
        return None

    # Threshold tripped. Reset so the note doesn't fire on every
    # subsequent failure too — the agent gets one nudge, not a
    # spammy stream.
    _consecutive_fails.pop(path, None)
    return AfterToolHookResult(
        extra_user_message=(
            f"{RECONSIDER_NOTE} (path={path}, "
            f"{CONSECUTIVE_FAILURE_THRESHOLD} consecutive failures)"
        )
    )


def register(api: Any) -> None:
    api.after_tool_call(_on_after_tool)


def _reset_for_tests() -> None:
    """Clear the per-path counter. Called by smoke tests so each test
    starts from a known state."""
    _consecutive_fails.clear()
