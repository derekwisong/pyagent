"""End-to-end smoke for the doc-tools plugin.

Six concerns:

  1. **Plugin discovers and registers both tools.** ``register()``
     publishes ``extract`` and ``summarize`` through the plugin API
     surface; callers can fetch them off the registered map.
  2. **Size-floor guardrail fires for small files.** A doc under
     ``min_size_chars`` returns a ``<file is N chars …>`` marker
     pointing the caller back to ``read_file`` instead of spinning
     up a sub-LLM.
  3. **Bigger doc invokes the sub-LLM and returns prefixed output.**
     Using the ``pyagent/echo`` stub (which returns the most-recent
     user message verbatim), we verify the document content and
     query made it into the LLM call and the prefix names the model.
  4. **Per-call ``model=`` override beats the configured model.**
  5. **Configured ``model`` value is honored when no override.**
  6. **Validation paths return string markers, not raises** —
     missing path, missing query, bogus ``max_chars``, nonexistent
     file, and a sub-LLM that fails all surface as ``<… error: …>``.

Run with:

    .venv/bin/python -m tests.smoke_doc_tools
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from pyagent import permissions
from pyagent.plugins import doc_tools as dt_mod


def _capture_tools() -> dict:
    """Run ``register()`` against a fake API; return the captured tools.

    The fake also lets us stub out ``plugin_config`` so individual
    checks can drive config-driven behavior without touching the real
    config.toml on disk.
    """
    captured: dict = {"tools": {}, "plugin_config": {}}

    class _FakeAPI:
        @property
        def plugin_config(self):
            return captured["plugin_config"]

        def register_tool(self, name, fn):
            captured["tools"][name] = fn

    dt_mod.register(_FakeAPI())
    return captured


def _check_register_publishes_both_tools() -> None:
    cap = _capture_tools()
    assert set(cap["tools"].keys()) == {"extract", "summarize"}, cap["tools"]
    print("✓ register() publishes extract + summarize")


def _check_size_floor_extract() -> None:
    """A small file gets the read_file pointer, not a sub-LLM call."""
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 1000}
    extract = cap["tools"]["extract"]

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write("tiny content " * 5)  # ~65 chars
        path = f.name

    permissions.pre_approve(path)
    try:
        out = extract(path, "extract anything")
    finally:
        Path(path).unlink()

    assert isinstance(out, str), type(out)
    assert out.startswith("<file is "), out
    assert "under 1000-char threshold" in out, out
    assert "read_file" in out, out
    print("✓ extract: small file → read_file pointer (no sub-LLM call)")


def _check_size_floor_summarize() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 1000}
    summarize = cap["tools"]["summarize"]

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write("tiny" * 10)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = summarize(path)
    finally:
        Path(path).unlink()

    assert out.startswith("<file is "), out
    assert "read_file" in out, out
    print("✓ summarize: small file → read_file pointer")


def _check_extract_invokes_subllm_via_echo_stub() -> None:
    """Bigger doc + echo stub: confirm content + query reached the LLM
    and the prefix names the model."""
    cap = _capture_tools()
    # Override-via-call so this check is independent of config.
    extract = cap["tools"]["extract"]

    body = "DOCSENTINEL " * 500  # well over the 4KB default floor
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(
            path,
            "QUERYSENTINEL: list every occurrence",
            model="pyagent/echo",
        )
    finally:
        Path(path).unlink()

    assert isinstance(out, str), type(out)
    assert out.startswith("[extracted via pyagent/echo]\n"), out
    # Echo returns the user message verbatim; the doc, query, and path
    # are all wrapped into that user message.
    assert "DOCSENTINEL" in out, out
    assert "QUERYSENTINEL" in out, out
    assert path in out, out
    print("✓ extract: big doc → sub-LLM call carries doc + query + path")


def _check_summarize_invokes_subllm_via_echo_stub() -> None:
    cap = _capture_tools()
    summarize = cap["tools"]["summarize"]

    body = "SUMMARY-SENTINEL " * 300
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = summarize(
            path,
            focus="FOCUSSENTINEL on key terms",
            max_chars=500,
            model="pyagent/echo",
        )
    finally:
        Path(path).unlink()

    assert out.startswith("[summarized via pyagent/echo]\n"), out
    assert "SUMMARY-SENTINEL" in out, out
    assert "FOCUSSENTINEL" in out, out
    assert "Summary budget: under 500 characters." in out, out
    print("✓ summarize: prefix + body + focus + budget reach the LLM")


def _check_per_call_model_overrides_config() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"model": "pyagent/loremipsum"}
    extract = cap["tools"]["extract"]

    body = "X" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        # Per-call override should win, not the configured loremipsum.
        out = extract(path, "anything", model="pyagent/echo")
    finally:
        Path(path).unlink()
    assert out.startswith("[extracted via pyagent/echo]\n"), out
    print("✓ per-call model= overrides plugin config")


def _check_configured_model_is_honored() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"model": "pyagent/echo"}
    extract = cap["tools"]["extract"]

    body = "Y" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(path, "anything")  # no override
    finally:
        Path(path).unlink()
    assert out.startswith("[extracted via pyagent/echo]\n"), out
    print("✓ configured model is used when no per-call override")


def _check_input_validation() -> None:
    cap = _capture_tools()
    extract = cap["tools"]["extract"]
    summarize = cap["tools"]["summarize"]

    # Missing path
    assert extract("", "q") == "<error: path is required>"
    assert summarize("") == "<error: path is required>"
    # Missing query
    assert extract("/tmp/anything.txt", "").startswith(
        "<error: query is required"
    )
    # Bad max_chars
    out = summarize("/tmp/whatever", max_chars="not-a-number")
    assert out.startswith("<error: max_chars must be an integer"), out
    out = summarize("/tmp/whatever", max_chars=10)
    assert out.startswith("<error: max_chars="), out
    print("✓ input validation: missing path/query, bogus max_chars")


def _check_nonexistent_file_marker() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 100}  # anything above zero
    extract = cap["tools"]["extract"]

    bogus = "/tmp/__doc_tools_smoke_does_not_exist__.txt"
    permissions.pre_approve(bogus)
    out = extract(bogus, "anything")
    assert out == f"<file not found: {bogus}>", out
    print("✓ nonexistent path → <file not found: ...> marker")


def _check_subllm_failure_wrapped() -> None:
    """A factory that raises (e.g. missing API key) becomes a tool-result
    error string, not a raise out of the tool."""
    cap = _capture_tools()
    extract = cap["tools"]["extract"]

    body = "Z" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)

    from pyagent import llms

    def _boom(model):
        raise RuntimeError("simulated provider unavailable")

    try:
        with mock.patch.object(llms, "get_client", side_effect=_boom):
            out = extract(path, "anything", model="pyagent/echo")
    finally:
        Path(path).unlink()

    assert out.startswith("<extract error:"), out
    assert "simulated provider unavailable" in out, out
    print("✓ sub-LLM construction failure → <extract error: ...> marker")


def main() -> None:
    _check_register_publishes_both_tools()
    _check_size_floor_extract()
    _check_size_floor_summarize()
    _check_extract_invokes_subllm_via_echo_stub()
    _check_summarize_invokes_subllm_via_echo_stub()
    _check_per_call_model_overrides_config()
    _check_configured_model_is_honored()
    _check_input_validation()
    _check_nonexistent_file_marker()
    _check_subllm_failure_wrapped()
    print("smoke_doc_tools: all checks passed")


if __name__ == "__main__":
    main()
