"""Smoke tests for the claude-code-cli plugin's early-return error paths.

All cases exercised here return before subprocess is invoked, so the
tests don't require the `claude` binary to be present on PATH. The
subprocess-execution paths (success, runtime error, timeout) are
out of scope — they're integration concerns.

Run with:

    .venv/bin/python -m tests.smoke_claude_code_cli
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

# Import the plugin module directly so we can drive its inner tool
# function without booting the full plugin loader (which gates on
# `claude` being on PATH via the manifest's [requires] binaries list).
PLUGIN_DIR = Path(
    "/home/derek/src/pyagent/pyagent/plugins/claude_code_cli"
)
spec = importlib.util.spec_from_file_location(
    "claude_code_cli_under_test",
    PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class _StubAPI:
    """Capture the registered tool function so we can call it directly."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def register_tool(self, name: str, fn) -> None:
        self.tools[name] = fn


def _get_tool():
    api = _StubAPI()
    mod.register(api)
    return api.tools["claude_code_cli"]


def test_empty_prompt_rejected() -> None:
    tool = _get_tool()
    out = tool(prompt="")
    assert out == "<prompt is empty>"
    out = tool(prompt="   \n\t")
    assert out == "<prompt is empty>"
    print("✓ empty / whitespace prompt rejected")


def test_bad_output_format_rejected() -> None:
    tool = _get_tool()
    out = tool(prompt="hi", output_format="yaml")
    assert "output_format must be one of" in out
    assert "'yaml'" in out
    print("✓ bad output_format rejected")


def test_empty_allow_tools_rejected() -> None:
    """Bug fix: empty list collides with argv parsing
    (`--allowedTools <prompt>`) so we reject it explicitly."""
    tool = _get_tool()
    out = tool(prompt="hi", allow_tools=[])
    assert "allow_tools must be None" in out
    assert "rejected" in out
    print("✓ empty allow_tools rejected")


def test_oversize_json_schema_rejected() -> None:
    """Schemas larger than _MAX_JSON_SCHEMA_CHARS are rejected before
    they ever hit argv (where they'd otherwise blow ARG_MAX)."""
    tool = _get_tool()
    huge = {"properties": {f"f{i}": {"type": "string"} for i in range(8000)}}
    out = tool(
        prompt="hi", output_format="json", json_schema=huge
    )
    assert "json_schema exceeds" in out
    assert "chars" in out
    print("✓ oversize json_schema rejected")


def test_unserializable_json_schema_rejected() -> None:
    tool = _get_tool()
    out = tool(
        prompt="hi",
        output_format="json",
        json_schema={"bad": object()},  # not JSON-serializable
    )
    assert "json_schema is not JSON-serializable" in out
    print("✓ unserializable json_schema rejected")


def test_oversize_context_file_rejected(tmp_path: Path | None = None) -> None:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="smoke-claude-cli-"))
    try:
        big = tmp / "big.txt"
        # Just over the cap — char-mode read makes this a 1-MiB+1-char
        # file. UTF-8 ASCII so byte count == char count for clarity.
        big.write_text("a" * (mod._MAX_CONTEXT_CHARS + 1))
        tool = _get_tool()
        out = tool(prompt="summarize", context_file=str(big))
        assert "context_file exceeds" in out
        assert "chars" in out
        print("✓ oversize context_file rejected")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_missing_context_file_rejected() -> None:
    tool = _get_tool()
    out = tool(prompt="hi", context_file="/nonexistent/path/xyz")
    assert "cannot read context_file" in out
    print("✓ missing context_file surfaces clean error")


# ---- post-subprocess paths: monkey-patch Popen to return a fake
# claude envelope so we can exercise the parse / log / extract logic
# without needing the real `claude` binary on PATH.


class _FakePopen:
    """Stand-in for subprocess.Popen that records the cmd it was given
    and returns a pre-canned (returncode, stdout, stderr) on
    communicate(). Mirrors only what claude_code_cli reads."""

    last_cmd: list[str] = []

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = None
        self.pid = 99999

    def communicate(self, input: str | None = None, timeout: float | None = None):
        self.returncode = self._returncode
        return self._stdout, self._stderr


def _install_fake_popen(returncode: int, stdout: str, stderr: str = ""):
    """Monkey-patch mod.subprocess.Popen and return a callable that
    captures the cmd argv. Caller must call .restore() to undo."""

    captured: dict = {"cmd": None}
    original = mod.subprocess.Popen

    def factory(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(returncode, stdout, stderr)

    mod.subprocess.Popen = factory  # type: ignore[assignment]

    class _Restore:
        cmd = captured

        def restore(self) -> None:
            mod.subprocess.Popen = original  # type: ignore[assignment]

    return _Restore()


_OK_ENVELOPE = (
    '{"type":"result","subtype":"success","is_error":false,'
    '"duration_ms":1234,"num_turns":1,'
    '"result":"the answer is 42",'
    '"session_id":"abc-123","total_cost_usd":0.00042,'
    '"usage":{"input_tokens":100,"output_tokens":50}}'
)


def test_text_mode_extracts_result_field() -> None:
    fake = _install_fake_popen(0, _OK_ENVELOPE)
    try:
        tool = _get_tool()
        out = tool(prompt="what is 6 times 7?", output_format="text")
        assert out.startswith("session: <one-off>\n\n")
        assert "the answer is 42" in out
        # Should NOT contain the JSON envelope itself in text mode.
        assert "total_cost_usd" not in out
        assert "session_id" not in out
        # cmd should always carry --output-format json now.
        cmd = fake.cmd["cmd"]
        assert cmd is not None
        assert "--output-format" in cmd
        assert cmd[cmd.index("--output-format") + 1] == "json"
        print("✓ text mode extracts envelope.result field")
    finally:
        fake.restore()


def test_json_mode_returns_envelope_verbatim() -> None:
    fake = _install_fake_popen(0, _OK_ENVELOPE)
    try:
        tool = _get_tool()
        out = tool(prompt="give me JSON", output_format="json")
        assert out.startswith("session: <one-off>\n\n")
        # Full envelope should be present, byte-for-byte.
        assert _OK_ENVELOPE in out
        print("✓ json mode returns claude's envelope verbatim")
    finally:
        fake.restore()


def test_is_error_envelope_surfaces_as_marker() -> None:
    err_env = (
        '{"type":"result","subtype":"error_during_execution",'
        '"is_error":true,"result":"rate limited",'
        '"session_id":"abc","total_cost_usd":0,"duration_ms":50,'
        '"num_turns":0,"usage":{"input_tokens":0,"output_tokens":0}}'
    )
    fake = _install_fake_popen(0, err_env)
    try:
        tool = _get_tool()
        out = tool(prompt="anything", output_format="text")
        assert out.startswith("<claude error: ")
        assert "rate limited" in out
        print("✓ is_error envelope surfaces as <claude error>")
    finally:
        fake.restore()


def test_invalid_json_stdout_returns_error() -> None:
    fake = _install_fake_popen(0, "not actually json {{{")
    try:
        tool = _get_tool()
        out = tool(prompt="hi", output_format="text")
        assert out.startswith("<claude error: invalid JSON envelope")
        # Preview of the bad output should be in the error.
        assert "not actually json" in out
        print("✓ non-JSON stdout returns clean error")
    finally:
        fake.restore()


def test_session_name_in_label() -> None:
    fake = _install_fake_popen(0, _OK_ENVELOPE)
    try:
        tool = _get_tool()
        out = tool(prompt="hi", session_name="threaded-call")
        assert out.startswith("session: threaded-call\n\n")
        # cmd should include --session-id with a UUID since this is a
        # fresh session_name.
        cmd = fake.cmd["cmd"]
        assert "--session-id" in cmd
        print("✓ session_name surfaces in label and --session-id flag")
    finally:
        fake.restore()


def main() -> None:
    test_empty_prompt_rejected()
    test_bad_output_format_rejected()
    test_empty_allow_tools_rejected()
    test_oversize_json_schema_rejected()
    test_unserializable_json_schema_rejected()
    test_oversize_context_file_rejected()
    test_missing_context_file_rejected()
    test_text_mode_extracts_result_field()
    test_json_mode_returns_envelope_verbatim()
    test_is_error_envelope_surfaces_as_marker()
    test_invalid_json_stdout_returns_error()
    test_session_name_in_label()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
