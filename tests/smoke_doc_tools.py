"""End-to-end smoke for the doc-tools plugin.

Concerns covered:

  1. **Plugin discovers and registers both tools.** ``register()``
     publishes ``extract_doc`` and ``summarize_doc`` through the
     plugin API surface; callers can fetch them off the registered map.
  2. **Size-floor guardrail fires for small files.** A doc under
     ``min_size_chars`` returns a ``<file is N chars …>`` marker
     pointing the caller back to ``read_file`` instead of spinning
     up a sub-LLM.
  3. **Max-size guardrail fires for huge files.** Over the 200K
     hard ceiling, the tool refuses with a "slice with read_file
     first" pointer.
  4. **Bigger doc invokes the sub-LLM and returns prefixed output.**
     Using the ``pyagent/echo`` stub (which returns the most-recent
     user message verbatim), we verify the document content and
     query made it into the LLM call and the prefix names the model.
  5. **Per-call ``model=`` override beats the configured model.**
  6. **Configured ``model`` value is honored when no override.**
  7. **Env var overrides config but loses to per-call.**
     ``PYAGENT_DOC_TOOLS_MODEL`` beats ``[plugins.doc-tools] model``
     and is beaten by an explicit ``model=`` argument.
  8. **Validation paths return string markers, not raises** —
     missing path, missing query, bogus ``max_chars``, nonexistent
     file, unicode-decode failures, permission denial, and a
     sub-LLM that fails all surface as ``<… error: …>``.
  9. **Schema validation** — non-string types, oversized strings,
     and non-JSON strings each return a typed error marker.
 10. **Sub-LLM timeout** — a hung sub-LLM call surfaces as a clean
     timeout marker, not an indefinite hang of the agent loop.
 11. **LRU cache** — identical re-queries of an unchanged file
     return without invoking the sub-LLM. Touching the file
     invalidates. Disabled with ``cache_size = 0``.

Run with:

    .venv/bin/python -m tests.smoke_doc_tools
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from unittest import mock

from pyagent import permissions
from pyagent.plugins import doc_tools as dt_mod


def _capture_tools(plugin_config: dict | None = None) -> dict:
    """Run ``register()`` against a fake API; return captured state.

    The fake stubs out ``plugin_config`` (driven by the optional arg
    or assigned to the returned dict before `register()` re-runs) and
    captures any ``api.log()`` calls so register-time warnings can be
    asserted. Each call also clears the module-level LRU cache so
    checks don't bleed into each other.

    Pass ``plugin_config`` for the single-call shape used by warning
    checks. For multi-step tests that mutate config between calls,
    omit the arg, mutate ``captured["plugin_config"]``, and re-run
    ``dt_mod.register()`` if needed.
    """
    dt_mod._cache_clear()
    captured: dict = {
        "tools": {},
        "plugin_config": plugin_config if plugin_config is not None else {},
        "logs": [],
    }

    class _FakeAPI:
        @property
        def plugin_config(self):
            return captured["plugin_config"]

        def register_tool(self, name, fn):
            captured["tools"][name] = fn

        def log(self, level, message):
            captured["logs"].append((level, message))

    dt_mod.register(_FakeAPI())
    return captured


def _check_register_publishes_both_tools() -> None:
    cap = _capture_tools()
    assert set(cap["tools"].keys()) == {"extract_doc", "summarize_doc"}, (
        cap["tools"]
    )
    print("✓ register() publishes extract_doc + summarize_doc")


def _check_size_floor_extract() -> None:
    """A small file gets the read_file pointer, not a sub-LLM call."""
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 1000}
    extract = cap["tools"]["extract_doc"]

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
    print("✓ extract_doc: small file → read_file pointer (no sub-LLM call)")


def _check_size_floor_summarize() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 1000}
    summarize = cap["tools"]["summarize_doc"]

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
    print("✓ summarize_doc: small file → read_file pointer")


def _check_max_size_guardrail() -> None:
    """Files over the 200K hard ceiling refuse without a sub-LLM call."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

    body = "X" * (dt_mod._MAX_DOC_CHARS + 1000)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(path, "anything", model="pyagent/echo")
    finally:
        Path(path).unlink()
    assert out.startswith("<file is "), out
    assert "over 200000-char limit" in out, out
    assert "slice with read_file" in out, out
    print("✓ extract_doc: over-size file → slice-first pointer")


def _check_extract_invokes_subllm_via_echo_stub() -> None:
    """Bigger doc + echo stub: confirm content + query reached the LLM
    and the prefix names the model."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

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
    assert "DOCSENTINEL" in out, out
    assert "QUERYSENTINEL" in out, out
    assert path in out, out
    print("✓ extract_doc: big doc → sub-LLM call carries doc + query + path")


def _check_summarize_invokes_subllm_via_echo_stub() -> None:
    cap = _capture_tools()
    summarize = cap["tools"]["summarize_doc"]

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
    print("✓ summarize_doc: prefix + body + focus + budget reach the LLM")


def _check_per_call_model_overrides_config() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"model": "pyagent/loremipsum"}
    extract = cap["tools"]["extract_doc"]

    body = "X" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(path, "anything", model="pyagent/echo")
    finally:
        Path(path).unlink()
    assert out.startswith("[extracted via pyagent/echo]\n"), out
    print("✓ per-call model= overrides plugin config")


def _check_env_var_overrides_config() -> None:
    """``PYAGENT_DOC_TOOLS_MODEL`` beats config; loses to per-call."""
    cap = _capture_tools()
    cap["plugin_config"] = {"model": "pyagent/loremipsum"}
    extract = cap["tools"]["extract_doc"]

    body = "X" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        # Env beats config.
        with mock.patch.dict(
            os.environ, {"PYAGENT_DOC_TOOLS_MODEL": "pyagent/echo"}
        ):
            out = extract(path, "anything")
        assert out.startswith("[extracted via pyagent/echo]\n"), out

        # Per-call still beats env. Both are set; explicit wins.
        with mock.patch.dict(
            os.environ, {"PYAGENT_DOC_TOOLS_MODEL": "pyagent/loremipsum"}
        ):
            out = extract(path, "anything", model="pyagent/echo")
        assert out.startswith("[extracted via pyagent/echo]\n"), out

        # Empty / whitespace env var falls through to config.
        with mock.patch.dict(os.environ, {"PYAGENT_DOC_TOOLS_MODEL": "   "}):
            out = extract(path, "anything")
        assert out.startswith("[extracted via pyagent/loremipsum]\n"), out
    finally:
        Path(path).unlink()
    print("✓ env var: beats config, loses to per-call, empty falls through")


def _check_configured_model_is_honored() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"model": "pyagent/echo"}
    extract = cap["tools"]["extract_doc"]

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
    extract = cap["tools"]["extract_doc"]
    summarize = cap["tools"]["summarize_doc"]

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


def _check_schema_validation() -> None:
    """Bad schema args produce typed error markers, not raises."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

    # Non-string schema (would f-string-coerce silently otherwise).
    out = extract("/tmp/x.txt", "q", schema={"a": 1})
    assert out.startswith("<error: schema must be a string"), out
    assert "dict" in out, out

    # Oversized schema.
    huge = '{"x":"' + "y" * (dt_mod._MAX_SCHEMA_CHARS + 100) + '"}'
    out = extract("/tmp/x.txt", "q", schema=huge)
    assert out.startswith("<error: schema is "), out
    assert "max 16000" in out, out

    # Non-JSON schema string.
    out = extract("/tmp/x.txt", "q", schema="not actually json {")
    assert out.startswith("<error: schema is not valid JSON"), out

    # Empty / whitespace schema is fine — treated as no schema. We
    # need a real file to get past the path check; use a body that
    # would actually invoke the LLM via the echo stub.
    body = "Z" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(path, "anything", schema="   ", model="pyagent/echo")
    finally:
        Path(path).unlink()
    assert out.startswith("[extracted via pyagent/echo]\n"), out
    # Whitespace schema must NOT inject the schema block.
    assert "schema" not in out.lower().split("\n", 1)[1].split("extraction request:")[0], out
    print("✓ schema validation: type/length/JSON-parse + empty falls through")


def _check_decode_error_marker() -> None:
    """A non-UTF-8 file surfaces as 'could not decode as text', not
    the misleading 'binary file' wording."""
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 100}
    extract = cap["tools"]["extract_doc"]

    with tempfile.NamedTemporaryFile(
        "wb", suffix=".bin", delete=False
    ) as f:
        # Latin-1-only bytes that fail UTF-8 decode.
        f.write(b"\xff\xfe\xfd this is not utf-8 \xc3\x28")
        path = f.name
    permissions.pre_approve(path)
    try:
        out = extract(path, "anything")
    finally:
        Path(path).unlink()
    assert out.startswith("<could not decode as text"), out
    assert "not utf-8" in out, out
    print("✓ decode error: <could not decode as text (not utf-8): ...>")


def _check_permission_denied_marker() -> None:
    """A path the permission gate rejects returns the access-denied
    marker without trying to read the file."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

    bogus = "/tmp/__doc_tools_smoke_perm_denied__.txt"
    with mock.patch.object(
        permissions, "require_access", return_value=False
    ):
        out = extract(bogus, "anything")
    assert out == f"<access denied: {bogus}>", out
    print("✓ permission denied → <access denied: ...> marker")


def _check_nonexistent_file_marker() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"min_size_chars": 100}
    extract = cap["tools"]["extract_doc"]

    bogus = "/tmp/__doc_tools_smoke_does_not_exist__.txt"
    permissions.pre_approve(bogus)
    out = extract(bogus, "anything")
    assert out == f"<file not found: {bogus}>", out
    print("✓ nonexistent path → <file not found: ...> marker")


def _check_subllm_failure_wrapped() -> None:
    """A factory that raises (e.g. missing API key) becomes a tool-result
    error string, not a raise out of the tool."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

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


def _check_subllm_timeout_marker() -> None:
    """A sub-LLM call that hangs longer than ``timeout_s`` surfaces a
    timeout marker. Verified by stubbing ``get_client`` with a client
    whose ``respond`` sleeps."""
    cap = _capture_tools()
    cap["plugin_config"] = {"timeout_s": 1}
    extract = cap["tools"]["extract_doc"]

    body = "T" * 8000
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)

    class _SlowClient:
        def respond(self, **_kw):
            # Block long enough to exceed the 1-second cap.
            time.sleep(5)
            return {"text": "would have been a real reply"}

    from pyagent import llms

    try:
        with mock.patch.object(
            llms, "get_client", return_value=_SlowClient()
        ):
            t0 = time.time()
            out = extract(path, "anything", model="pyagent/echo")
            elapsed = time.time() - t0
    finally:
        Path(path).unlink()

    assert out.startswith("<extract error:"), out
    assert "timed out after 1s" in out, out
    # Tool returned within the deadline (allow 0.5s slack for
    # thread join + setup); did not block until the 5s sleep finished.
    assert elapsed < 2.5, f"timeout enforcement was slow: {elapsed:.2f}s"
    print(f"✓ sub-LLM timeout: <extract error: ... timed out> in {elapsed:.2f}s")


def _check_register_warns_on_bogus_model() -> None:
    """A malformed model string in config logs a warning at register
    time and still publishes the tools."""
    cap = _capture_tools(plugin_config={"model": "claude-haiku-no-slash"})
    assert "extract_doc" in cap["tools"], cap["tools"]
    assert "summarize_doc" in cap["tools"], cap["tools"]
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("'claude-haiku-no-slash'" in m for m in msgs), msgs
    assert any("provider/model" in m for m in msgs), msgs

    # Empty-half model: ``"anthropic/"`` shouldn't slip through.
    cap = _capture_tools(plugin_config={"model": "anthropic/"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("empty provider or model name" in m for m in msgs), msgs

    # Non-string model.
    cap = _capture_tools(plugin_config={"model": 42})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("model must be a string" in m for m in msgs), msgs
    print("✓ register-time warning: bogus model values flagged, tools still register")


def _check_register_warns_on_bogus_timeout() -> None:
    cap = _capture_tools(plugin_config={"timeout_s": "not-an-int"})
    assert "extract_doc" in cap["tools"], cap["tools"]
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be a positive integer" in m for m in msgs), msgs

    cap = _capture_tools(plugin_config={"timeout_s": 0})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be > 0" in m for m in msgs), msgs

    cap = _capture_tools(plugin_config={"timeout_s": -5})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("timeout_s must be > 0, got -5" in m for m in msgs), msgs
    print("✓ register-time warning: bogus timeout_s values flagged")


def _check_register_warns_on_bogus_cache_size() -> None:
    cap = _capture_tools(plugin_config={"cache_size": "huge"})
    assert "extract_doc" in cap["tools"], cap["tools"]
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("cache_size must be a non-negative integer" in m for m in msgs), msgs

    cap = _capture_tools(plugin_config={"cache_size": -1})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert any("cache_size must be >= 0, got -1" in m for m in msgs), msgs
    print("✓ register-time warning: bogus cache_size values flagged")


def _check_register_silent_on_valid_config() -> None:
    """A clean config produces zero warnings."""
    cap = _capture_tools(plugin_config={
        "model": "anthropic/claude-haiku-4-5-20251001",
        "timeout_s": 60,
        "cache_size": 32,
        "min_size_chars": 4000,
    })
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"expected no warnings on clean config, got: {msgs}"

    # Empty config also produces zero warnings (every key is optional).
    cap = _capture_tools(plugin_config={})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"expected no warnings on empty config, got: {msgs}"

    # Plugin-provider shorthand (``ollama``) is accepted silently —
    # we can't verify plugin providers at register time, so we don't
    # warn on them.
    cap = _capture_tools(plugin_config={"model": "ollama"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"ollama shorthand should not warn, got: {msgs}"

    cap = _capture_tools(plugin_config={"model": "ollama/llama3.2:latest"})
    msgs = [m for level, m in cap["logs"] if level == "warning"]
    assert msgs == [], f"ollama/<model> should not warn, got: {msgs}"
    print("✓ register-time silent on valid config and plugin-provider shorthand")


def _check_lru_cache_returns_cached_result() -> None:
    """Identical re-query of an unchanged file returns the cached
    result without re-invoking the sub-LLM."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

    body = "CACHE-SENTINEL " * 500
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)

    from pyagent import llms

    real_get_client = llms.get_client
    call_count = {"n": 0}

    def _counted(model):
        call_count["n"] += 1
        return real_get_client(model)

    try:
        with mock.patch.object(llms, "get_client", side_effect=_counted):
            first = extract(path, "what's the sentinel?", model="pyagent/echo")
            second = extract(path, "what's the sentinel?", model="pyagent/echo")
    finally:
        Path(path).unlink()

    assert first == second, (first, second)
    assert call_count["n"] == 1, (
        f"expected 1 sub-LLM call (cache hit on second), got {call_count['n']}"
    )
    print("✓ cache: identical re-query hits cache (1 call instead of 2)")


def _check_lru_cache_invalidates_on_file_change() -> None:
    """Touching the file's mtime invalidates the cached entry."""
    cap = _capture_tools()
    extract = cap["tools"]["extract_doc"]

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write("ORIGINAL " * 600)
        path = f.name
    permissions.pre_approve(path)

    from pyagent import llms

    real_get_client = llms.get_client
    call_count = {"n": 0}

    def _counted(model):
        call_count["n"] += 1
        return real_get_client(model)

    try:
        with mock.patch.object(llms, "get_client", side_effect=_counted):
            extract(path, "q", model="pyagent/echo")
            # Modify content + bump mtime so signature changes.
            time.sleep(0.01)
            Path(path).write_text("CHANGED " * 600)
            extract(path, "q", model="pyagent/echo")
    finally:
        Path(path).unlink()

    assert call_count["n"] == 2, (
        f"expected 2 sub-LLM calls (file changed → cache miss), "
        f"got {call_count['n']}"
    )
    print("✓ cache: file mtime change invalidates entry")


def _check_lru_cache_disabled_at_size_zero() -> None:
    cap = _capture_tools()
    cap["plugin_config"] = {"cache_size": 0}
    extract = cap["tools"]["extract_doc"]

    body = "DISABLED " * 600
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as f:
        f.write(body)
        path = f.name
    permissions.pre_approve(path)

    from pyagent import llms

    real_get_client = llms.get_client
    call_count = {"n": 0}

    def _counted(model):
        call_count["n"] += 1
        return real_get_client(model)

    try:
        with mock.patch.object(llms, "get_client", side_effect=_counted):
            extract(path, "q", model="pyagent/echo")
            extract(path, "q", model="pyagent/echo")
    finally:
        Path(path).unlink()

    assert call_count["n"] == 2, (
        f"expected 2 sub-LLM calls (cache disabled), got {call_count['n']}"
    )
    print("✓ cache: cache_size=0 disables, both calls go to LLM")


def _check_lru_cache_evicts_oldest() -> None:
    """With cache_size=2, the third distinct entry evicts the first."""
    cap = _capture_tools()
    cap["plugin_config"] = {"cache_size": 2}
    extract = cap["tools"]["extract_doc"]

    paths: list[str] = []
    body = "LRU " * 1500
    for _ in range(3):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False
        ) as f:
            f.write(body)
            paths.append(f.name)
        permissions.pre_approve(paths[-1])

    from pyagent import llms

    real_get_client = llms.get_client
    call_count = {"n": 0}

    def _counted(model):
        call_count["n"] += 1
        return real_get_client(model)

    try:
        with mock.patch.object(llms, "get_client", side_effect=_counted):
            extract(paths[0], "q", model="pyagent/echo")
            extract(paths[1], "q", model="pyagent/echo")
            extract(paths[2], "q", model="pyagent/echo")
            # paths[0] was evicted; re-querying it must call again.
            extract(paths[0], "q", model="pyagent/echo")
            # paths[2] is still warm; re-query is a hit.
            extract(paths[2], "q", model="pyagent/echo")
    finally:
        for p in paths:
            Path(p).unlink()

    # 3 distinct + 1 eviction-replay + 1 hit = 4 calls
    assert call_count["n"] == 4, (
        f"expected 4 sub-LLM calls (3 distinct, 1 evicted-replay, "
        f"1 hit), got {call_count['n']}"
    )
    print("✓ cache: LRU evicts oldest at cap; warm entries still hit")


def main() -> None:
    # Ensure the env var isn't bleeding in from the runner's shell —
    # several checks assume "no env override" as their baseline.
    os.environ.pop("PYAGENT_DOC_TOOLS_MODEL", None)

    _check_register_publishes_both_tools()
    _check_size_floor_extract()
    _check_size_floor_summarize()
    _check_max_size_guardrail()
    _check_extract_invokes_subllm_via_echo_stub()
    _check_summarize_invokes_subllm_via_echo_stub()
    _check_per_call_model_overrides_config()
    _check_env_var_overrides_config()
    _check_configured_model_is_honored()
    _check_input_validation()
    _check_schema_validation()
    _check_decode_error_marker()
    _check_permission_denied_marker()
    _check_nonexistent_file_marker()
    _check_subllm_failure_wrapped()
    _check_subllm_timeout_marker()
    _check_register_warns_on_bogus_model()
    _check_register_warns_on_bogus_timeout()
    _check_register_warns_on_bogus_cache_size()
    _check_register_silent_on_valid_config()
    _check_lru_cache_returns_cached_result()
    _check_lru_cache_invalidates_on_file_change()
    _check_lru_cache_disabled_at_size_zero()
    _check_lru_cache_evicts_oldest()
    print("smoke_doc_tools: all checks passed")


if __name__ == "__main__":
    main()
