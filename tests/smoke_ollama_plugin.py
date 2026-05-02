"""End-to-end smoke for the bundled `ollama` plugin.

Concerns:

  1. **Default config carries it.** The plugin loads under default
     ``built_in_plugins_enabled`` and registers the ``ollama``
     provider plus the ``list_ollama_models`` tool — no env, no
     network.
  2. **Lazy network.** Plugin load and ``OllamaClient.__init__`` are
     network-free; only ``respond()`` and ``list_ollama_models`` ever
     hit the wire. Verified by leaving ``requests`` un-mocked during
     load and only patching for the call paths.
  3. **Provider routing wires up.** ``get_client("ollama/<name>")``
     returns an ``OllamaClient`` whose ``provider_model`` is
     ``ollama/<name>``. ``get_client("ollama")`` (no model) raises a
     clear, action-oriented error so users with no ``OLLAMA_MODEL``
     get a useful message at call time instead of a silent default.
  4. **OLLAMA_MODEL feeds the spec.** When the env is set,
     ``resolve_model("ollama")`` fills the default; when unset, it
     resolves to ``"ollama/"`` and the factory raises.
  5. **Wire shape is right.** ``respond()`` translates pyagent's
     internal conversation into Ollama's ``/api/chat`` JSON: system
     prefix, user/assistant/tool roles, ``tools`` block, ``stream:
     False``. The response path synthesizes ``call_<i>`` ids,
     normalises JSON-string arguments, and surfaces token counts.
  6. **OLLAMA_HOST normalisation.** Bare ``host:port`` upgrades to
     ``http://host:port`` and a trailing slash is stripped — matches
     what the Ollama CLI accepts.
  7. **list_ollama_models formatting.** Tags response renders as
     markdown bullets with size; an unreachable server yields the
     ``<ollama error: ...>`` marker rather than raising.
  8. **HTTP error body propagates.** A 4xx from ``/api/chat`` carries
     Ollama's JSON ``error`` body into the raised ``HTTPError`` so
     callers see the actual cause, not just a bare status code.
  9. **No-tools auto-retry.** When a model 400s with ``does not
     support tools``, the client transparently retries without tools,
     latches ``_skip_tools`` so subsequent turns skip the failed
     round trip, and propagates non-tools 4xx errors unchanged.

Run with:

    .venv/bin/python -m tests.smoke_ollama_plugin
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from pyagent import (
    config as config_mod,
    llms,
    paths as paths_mod,
    plugins,
)
from pyagent.llms import get_client, resolve_model
from pyagent.plugins.ollama import client as ollama_client_mod


def _check(label: str, cond: bool, detail: str = "") -> None:
    sym = "✓" if cond else "✗"
    extra = f" — {detail}" if detail else ""
    print(f"{sym} {label}{extra}")
    if not cond:
        raise SystemExit(1)


def _check_default_config_lists_ollama() -> None:
    """`ollama` is shipped in built_in_plugins_enabled."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-ollama-"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            cfg = config_mod.load()
    _check(
        "ollama in built_in_plugins_enabled",
        "ollama" in cfg["built_in_plugins_enabled"],
        repr(cfg["built_in_plugins_enabled"]),
    )


def _check_plugin_loads_and_registers() -> None:
    """Plugin loads under default config; provider + tool register;
    no network is touched during load."""
    tmp = Path(tempfile.mkdtemp(prefix="pyagent-smoke-ollama-"))
    # Patch requests inside the ollama client module to a sentinel
    # that raises if called — proves load() never hits the wire.
    sentinel = mock.MagicMock(side_effect=AssertionError("network during load"))
    with mock.patch.object(paths_mod, "config_dir", return_value=tmp):
        with mock.patch.object(
            plugins, "LOCAL_PLUGINS_DIR", Path(tmp / "no_local_plugins")
        ):
            with mock.patch.object(ollama_client_mod, "requests", sentinel):
                # Also ensure OLLAMA_MODEL isn't set so default_model="".
                with mock.patch.dict("os.environ", {}, clear=False):
                    import os as _os
                    _os.environ.pop("OLLAMA_MODEL", None)
                    loaded = plugins.load()

    plugin_names = [s.manifest.name for s in loaded.states]
    _check("ollama plugin loaded", "ollama" in plugin_names, repr(plugin_names))
    _check(
        "ollama in LoadedPlugins.providers()",
        "ollama" in loaded.providers(),
        repr(list(loaded.providers())),
    )
    _check(
        "ollama published to llms._PLUGIN_PROVIDERS",
        "ollama" in llms._PLUGIN_PROVIDERS,
    )
    _check(
        "list_ollama_models tool registered",
        "list_ollama_models" in loaded.tools(),
        repr(list(loaded.tools())),
    )


def _check_get_client_with_explicit_model() -> None:
    plugins.load()
    client = get_client("ollama/llama3.2")
    _check(
        "OllamaClient.provider_model is ollama/llama3.2",
        client.provider_model == "ollama/llama3.2",
        client.provider_model,
    )
    _check("OllamaClient.model is llama3.2", client.model == "llama3.2")


def _check_get_client_without_model_raises() -> None:
    """`--model ollama` (no slash) with no OLLAMA_MODEL → clear error
    only at call time, never at load time."""
    import os as _os
    saved = _os.environ.pop("OLLAMA_MODEL", None)
    try:
        plugins.load()  # must not raise even though no default model
        raised = False
        try:
            get_client("ollama")
        except ValueError as e:
            raised = True
            msg = str(e)
            _check(
                "missing-model error mentions OLLAMA_MODEL",
                "OLLAMA_MODEL" in msg,
                msg,
            )
            _check(
                "missing-model error mentions list_ollama_models",
                "list_ollama_models" in msg,
                msg,
            )
        _check("get_client('ollama') raised when no default", raised)
    finally:
        if saved is not None:
            _os.environ["OLLAMA_MODEL"] = saved


def _check_ollama_model_env_feeds_default() -> None:
    """`OLLAMA_MODEL=foo` → resolve_model('ollama') == 'ollama/foo'."""
    import os as _os
    saved = _os.environ.get("OLLAMA_MODEL")
    _os.environ["OLLAMA_MODEL"] = "qwen2.5"
    try:
        plugins.load()
        resolved = resolve_model("ollama")
        _check(
            "resolve_model('ollama') honors OLLAMA_MODEL",
            resolved == "ollama/qwen2.5",
            resolved,
        )
    finally:
        if saved is None:
            _os.environ.pop("OLLAMA_MODEL", None)
        else:
            _os.environ["OLLAMA_MODEL"] = saved
    # Re-load with env unset so later tests see a clean default.
    _os.environ.pop("OLLAMA_MODEL", None)
    plugins.load()


def _check_resolve_host_normalises() -> None:
    import os as _os
    saved = _os.environ.get("OLLAMA_HOST")

    _os.environ["OLLAMA_HOST"] = "remote-box:11434"
    _check(
        "OLLAMA_HOST without scheme upgrades to http://",
        ollama_client_mod._resolve_host() == "http://remote-box:11434",
        ollama_client_mod._resolve_host(),
    )

    _os.environ["OLLAMA_HOST"] = "https://ollama.example.com/"
    _check(
        "trailing slash stripped",
        ollama_client_mod._resolve_host() == "https://ollama.example.com",
        ollama_client_mod._resolve_host(),
    )

    _os.environ.pop("OLLAMA_HOST", None)
    _check(
        "default host when env unset",
        ollama_client_mod._resolve_host() == "http://localhost:11434",
        ollama_client_mod._resolve_host(),
    )

    if saved is None:
        _os.environ.pop("OLLAMA_HOST", None)
    else:
        _os.environ["OLLAMA_HOST"] = saved


class _FakeResponse:
    def __init__(
        self,
        payload: dict | None = None,
        status_code: int = 200,
        text: str | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        # `text` overrides JSON for cases where the body isn't valid
        # JSON — exercises the fallback in `_raise_with_body`.
        self._text = text

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        if self._text is not None:
            return self._text
        if self._payload is None:
            return ""
        import json as _json
        return _json.dumps(self._payload)

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _check_respond_request_shape_and_response_parse() -> None:
    """End-to-end mocked turn — verifies request body and response
    parsing including tool-call id synthesis and string-args
    normalisation."""
    captured: dict = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["timeout"] = timeout
        return _FakeResponse(
            {
                "message": {
                    "role": "assistant",
                    "content": "thinking…",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "lookup",
                                # Stringified arguments — older models do this.
                                "arguments": '{"q": "weather"}',
                            }
                        },
                        {
                            "function": {
                                "name": "noop",
                                "arguments": {"x": 1},
                            }
                        },
                    ],
                },
                "prompt_eval_count": 42,
                "eval_count": 7,
            }
        )

    client = ollama_client_mod.OllamaClient(model="llama3.2", host="http://h:1")
    with mock.patch.object(ollama_client_mod.requests, "post", side_effect=fake_post):
        out = client.respond(
            conversation=[
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "text": "calling lookup",
                    "tool_calls": [
                        {"id": "abc", "name": "lookup", "args": {"q": "weather"}}
                    ],
                },
                {
                    "role": "user",
                    "tool_results": [
                        {"id": "abc", "name": "lookup", "content": "sunny"}
                    ],
                },
            ],
            system="STABLE",
            system_volatile="VOL",
            tools=[
                {
                    "name": "lookup",
                    "description": "Look stuff up",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )

    body = captured["body"]
    _check("POST hit /api/chat", captured["url"].endswith("/api/chat"), captured["url"])
    _check("stream is False", body["stream"] is False)
    _check("model carried through", body["model"] == "llama3.2", body["model"])

    msgs = body["messages"]
    _check(
        "system block prefixes conversation",
        msgs[0] == {"role": "system", "content": "STABLE\n\nVOL"},
        repr(msgs[0]),
    )
    _check("user message present", msgs[1] == {"role": "user", "content": "hello"})
    _check("assistant content carried", msgs[2]["role"] == "assistant")
    _check(
        "assistant tool_calls translated",
        msgs[2]["tool_calls"][0]["function"]["name"] == "lookup",
        repr(msgs[2]),
    )
    _check(
        "tool result becomes role=tool",
        msgs[3]["role"] == "tool" and msgs[3]["tool_name"] == "lookup",
        repr(msgs[3]),
    )
    _check(
        "tools block formatted as openai-shaped function decls",
        body["tools"][0]["function"]["name"] == "lookup",
        repr(body["tools"]),
    )

    _check("response text parsed", out["text"] == "thinking…", repr(out["text"]))
    _check("two tool_calls returned", len(out["tool_calls"]) == 2)
    _check(
        "first call id synthesized as call_0",
        out["tool_calls"][0]["id"] == "call_0",
        repr(out["tool_calls"][0]),
    )
    _check(
        "stringified arguments normalised to dict",
        out["tool_calls"][0]["args"] == {"q": "weather"},
        repr(out["tool_calls"][0]["args"]),
    )
    _check(
        "dict arguments passed through unchanged",
        out["tool_calls"][1]["args"] == {"x": 1},
        repr(out["tool_calls"][1]["args"]),
    )
    _check(
        "usage carries token counts",
        out["usage"]["input"] == 42 and out["usage"]["output"] == 7,
        repr(out["usage"]),
    )
    _check(
        "usage cache fields zeroed (no Ollama prompt cache)",
        out["usage"]["cache_creation"] == 0 and out["usage"]["cache_read"] == 0,
    )
    _check(
        "usage.model is provider/model",
        out["usage"]["model"] == "ollama/llama3.2",
        out["usage"]["model"],
    )


def _check_list_ollama_models_formatting() -> None:
    payload = {
        "models": [
            {"name": "llama3.2:latest", "size": 2 * 1024**3},
            {"name": "qwen2.5:1.5b", "size": 950 * 1024**2},
            {"name": "tiny", "size": 0},
        ]
    }
    from pyagent.plugins.ollama import register as register_plugin

    captured: dict = {}

    class _FakeAPI:
        def register_provider(self, *a, **kw):
            pass

        def register_tool(self, name, fn):
            captured[name] = fn

    register_plugin(_FakeAPI())
    list_tool = captured["list_ollama_models"]

    with mock.patch.object(
        ollama_client_mod.requests,
        "get",
        return_value=_FakeResponse(payload),
    ):
        out = list_tool()
    _check("output is markdown bullets", out.startswith("- "), repr(out))
    _check("size in GB rendered", "2.0 GB" in out, out)
    _check("size in MB rendered", "950 MB" in out, out)
    _check("zero-size omits parenthetical", out.rstrip().endswith("- tiny"), out)


def _check_list_ollama_models_error_path() -> None:
    from pyagent.plugins.ollama import register as register_plugin

    captured: dict = {}

    class _FakeAPI:
        def register_provider(self, *a, **kw):
            pass

        def register_tool(self, name, fn):
            captured[name] = fn

    register_plugin(_FakeAPI())
    list_tool = captured["list_ollama_models"]

    def boom(*a, **kw):
        raise ConnectionError("connection refused")

    with mock.patch.object(ollama_client_mod.requests, "get", side_effect=boom):
        out = list_tool()
    _check(
        "unreachable server → <ollama error: ...> marker",
        out.startswith("<ollama error:") and "connection refused" in out,
        out,
    )


def _check_list_ollama_models_empty() -> None:
    from pyagent.plugins.ollama import register as register_plugin

    captured: dict = {}

    class _FakeAPI:
        def register_provider(self, *a, **kw):
            pass

        def register_tool(self, name, fn):
            captured[name] = fn

    register_plugin(_FakeAPI())
    list_tool = captured["list_ollama_models"]

    with mock.patch.object(
        ollama_client_mod.requests,
        "get",
        return_value=_FakeResponse({"models": []}),
    ):
        out = list_tool()
    _check(
        "empty server → <no models installed> marker",
        out.strip() == "<no models installed>",
        out,
    )


def _check_http_error_surfaces_ollama_body() -> None:
    """A 400 from `/api/chat` must carry Ollama's `error` body into
    the raised exception. Without this, vision/embedding models that
    reject the `tools` field surface as a bare HTTP 400 with no clue
    about the cause."""
    import requests as _requests

    fail = _FakeResponse(
        payload={"error": "registry/llama3.2-vision:11b does not support tools"},
        status_code=400,
    )
    client = ollama_client_mod.OllamaClient(model="llama3.2-vision:11b")
    with mock.patch.object(
        ollama_client_mod.requests, "post", return_value=fail
    ):
        try:
            client.respond(conversation=[{"role": "user", "content": "hi"}])
        except _requests.HTTPError as e:
            msg = str(e)
            _check(
                "exception message includes Ollama's error string",
                "does not support tools" in msg,
                msg,
            )
            _check(
                "exception message names the endpoint",
                "/api/chat" in msg,
                msg,
            )
            _check(
                "exception message names the status code",
                "400" in msg,
                msg,
            )
        else:
            _check("HTTPError raised on 400", False)

    # Non-JSON body (e.g. nginx HTML error page) falls back to the
    # raw text rather than blowing up.
    fail_html = _FakeResponse(
        payload=None, status_code=502, text="<html>bad gateway</html>"
    )
    with mock.patch.object(
        ollama_client_mod.requests, "get", return_value=fail_html
    ):
        try:
            ollama_client_mod.list_models()
        except _requests.HTTPError as e:
            _check(
                "non-JSON body falls back to raw text in error",
                "bad gateway" in str(e),
                str(e),
            )
        else:
            _check("HTTPError raised on 502 with HTML body", False)


def _check_provider_list_models_hook() -> None:
    """The ollama provider exposes a `list_models` callable on its
    ProviderSpec — that's how `pyagent --list-models` discovers what
    a remote Ollama has pulled, and how it tags each model's
    capabilities."""
    plugins.load()
    spec = llms._PLUGIN_PROVIDERS["ollama"]
    _check(
        "ollama ProviderSpec carries list_models",
        spec.list_models is not None,
    )

    tags_payload = {
        "models": [
            {"name": "llama3.2:latest"},
            {"name": "llava:7b"},
            {"name": ""},  # filtered: blank-name entries dropped
        ]
    }

    def fake_post(url, json=None, timeout=None):
        # /api/show payload — capabilities array varies per model so
        # we exercise both the tool and vision tag paths plus the
        # ``completion`` filter.
        name = (json or {}).get("name", "")
        if "llama3.2" in name:
            return _FakeResponse(
                {"capabilities": ["completion", "tools"]}
            )
        if "llava" in name:
            return _FakeResponse(
                {"capabilities": ["completion", "vision"]}
            )
        return _FakeResponse({"capabilities": []})

    with mock.patch.object(
        ollama_client_mod.requests,
        "get",
        return_value=_FakeResponse(tags_payload),
    ):
        with mock.patch.object(
            ollama_client_mod.requests, "post", side_effect=fake_post
        ):
            infos = spec.list_models()

    _check(
        "list_models returns ModelInfo records",
        all(isinstance(m, llms.ModelInfo) for m in infos),
        repr(infos),
    )
    by_name = {m.name: m for m in infos}
    _check(
        "blank-name entries filtered",
        list(by_name) == ["llama3.2:latest", "llava:7b"],
        repr(list(by_name)),
    )
    _check(
        "tool-capable model gets ('tools',) capability",
        by_name["llama3.2:latest"].capabilities == ("tools",),
        repr(by_name["llama3.2:latest"]),
    )
    _check(
        "vision model gets ('vision',) capability",
        by_name["llava:7b"].capabilities == ("vision",),
        repr(by_name["llava:7b"]),
    )

    # An /api/show failure for one model must not blank capabilities
    # for siblings — the model still appears, just without tags.
    def half_broken(url, json=None, timeout=None):
        if (json or {}).get("name") == "llava:7b":
            raise ConnectionError("model gone")
        return _FakeResponse({"capabilities": ["tools"]})

    with mock.patch.object(
        ollama_client_mod.requests,
        "get",
        return_value=_FakeResponse(tags_payload),
    ):
        with mock.patch.object(
            ollama_client_mod.requests, "post", side_effect=half_broken
        ):
            infos = spec.list_models()
    by_name = {m.name: m for m in infos}
    _check(
        "per-model show failure leaves the model in the list",
        "llava:7b" in by_name,
        repr(list(by_name)),
    )
    _check(
        "per-model show failure → empty capabilities, not raise",
        by_name["llava:7b"].capabilities == (),
        repr(by_name["llava:7b"]),
    )

    # The initial /api/tags failure does still raise — that's the
    # signal the aggregator turns into "(unavailable: ...)".
    def boom(*a, **kw):
        raise ConnectionError("refused")

    with mock.patch.object(ollama_client_mod.requests, "get", side_effect=boom):
        try:
            spec.list_models()
        except ConnectionError as e:
            _check(
                "list_models raises on /api/tags connection failure",
                "refused" in str(e),
            )
        else:
            _check("list_models raised on /api/tags failure", False)


def _check_no_tools_auto_retry() -> None:
    """When a model 400s with `does not support tools`, the client
    must transparently retry without tools, latch the decision so
    later turns skip tools, and not double-fail on success."""
    import copy as _copy

    import requests as _requests

    error_resp = _FakeResponse(
        payload={"error": "model llava:7b does not support tools"},
        status_code=400,
    )
    success_resp = _FakeResponse(
        payload={
            "message": {"role": "assistant", "content": "ok", "tool_calls": []},
            "prompt_eval_count": 1,
            "eval_count": 1,
        }
    )

    posts: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        # Deep-copy: respond() mutates the dict in place during the
        # tools-stripping retry, so a shallow capture would lose the
        # pre-mutation state.
        posts.append(_copy.deepcopy(json))
        if "tools" in (json or {}):
            return error_resp
        return success_resp

    client = ollama_client_mod.OllamaClient(model="llava:7b")
    with mock.patch.object(
        ollama_client_mod.requests, "post", side_effect=fake_post
    ):
        out = client.respond(
            conversation=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "name": "noop",
                    "description": "x",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
    _check(
        "first turn: two POSTs (tools then retry)",
        len(posts) == 2,
        repr(posts),
    )
    _check(
        "first POST included tools",
        "tools" in posts[0],
        repr(posts[0]),
    )
    _check(
        "retry POST stripped tools",
        "tools" not in posts[1],
        repr(posts[1]),
    )
    _check("retry returned success body", out["text"] == "ok", repr(out))
    _check(
        "client latched the no-tools decision",
        client._skip_tools is True,
    )

    # Second turn should skip tools up-front (no failed round trip).
    posts.clear()
    with mock.patch.object(
        ollama_client_mod.requests, "post", side_effect=fake_post
    ):
        client.respond(
            conversation=[{"role": "user", "content": "again"}],
            tools=[
                {
                    "name": "noop",
                    "description": "x",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
    _check(
        "second turn: single POST (latch held)",
        len(posts) == 1,
        repr(posts),
    )
    _check(
        "second POST has no tools",
        "tools" not in posts[0],
        repr(posts[0]),
    )

    # And a non-tools 400 must still propagate — we don't blanket-
    # retry every error.
    other_error = _FakeResponse(
        payload={"error": "model not found"}, status_code=404
    )
    fresh = ollama_client_mod.OllamaClient(model="nope")
    with mock.patch.object(
        ollama_client_mod.requests, "post", return_value=other_error
    ):
        try:
            fresh.respond(conversation=[{"role": "user", "content": "x"}])
        except _requests.HTTPError as e:
            _check(
                "non-tools 4xx still propagates",
                "model not found" in str(e),
                str(e),
            )
        else:
            _check("non-tools 4xx propagated", False)


def main() -> None:
    _check_default_config_lists_ollama()
    _check_plugin_loads_and_registers()
    _check_get_client_with_explicit_model()
    _check_get_client_without_model_raises()
    _check_ollama_model_env_feeds_default()
    _check_resolve_host_normalises()
    _check_respond_request_shape_and_response_parse()
    _check_list_ollama_models_formatting()
    _check_list_ollama_models_error_path()
    _check_list_ollama_models_empty()
    _check_http_error_surfaces_ollama_body()
    _check_no_tools_auto_retry()
    _check_provider_list_models_hook()
    print("smoke_ollama_plugin: all checks passed")


if __name__ == "__main__":
    main()
