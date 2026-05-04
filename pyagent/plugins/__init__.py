"""Plugin system for pyagent.

A plugin is a Python module that extends pyagent at runtime — it can
register tools, contribute prompt sections, and observe the
conversation loop. See `docs/plugin-design.md` for the full design.

Discovery is three-tier (later tier wins on name collision):

  1. <package>/plugins/<name>/         — bundled with pyagent
  2. (entry points)                    — pip-installed third-party
  3. <config-dir>/plugins/<name>/      — user-installed
  4. ./.pyagent/plugins/<name>/        — project-local

Within each tier, plugins load in sorted directory-name order. The
manifest's `name` is the plugin's identity; the directory name is
disk layout. They can differ, which is how users put numeric prefixes
on directories to control load order without renaming the plugin.

Each drop-in plugin directory contains:
  - manifest.toml: metadata (name, version, [provides], [load], …)
  - plugin.py:     Python module with `def register(api)` entrypoint
  - (optional helper modules and data files alongside)

Drop-ins are loaded via `importlib.util.spec_from_file_location` with
`submodule_search_locations` set to the plugin directory, so
`from . import helper` inside `plugin.py` resolves to siblings without
needing a top-level `__init__.py`. Subdirectories the plugin treats as
Python subpackages still need their own `__init__.py` per standard
Python rules.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import os
import shutil
import sys
import threading
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, Mapping

from pyagent import config, paths

logger = logging.getLogger(__name__)

LOCAL_PLUGINS_DIR = Path(".pyagent") / "plugins"
PACKAGE_PLUGINS_PKG = "pyagent.plugins"
ENTRY_POINT_GROUP = "pyagent.plugins"
# Set of plugin API versions this build of pyagent understands. v1
# plugins are observers (return values ignored); v2 plugins can return
# `ToolHookResult` / `AfterToolHookResult` from before_tool / after_tool
# to direct flow (block, mutate args, replace results, inject
# user-role messages).
SUPPORTED_API_VERSIONS: set[str] = {"1", "2"}
RECENT_MESSAGES_WINDOW = 8

# Maximum nesting depth for `PluginAPI.call_tool` chains. A → B → C → D
# is fine; A → B → C → D → E is rejected with the depth-exceeded marker.
# Cap is per-thread (we use threading.local) so concurrent agent threads
# don't see each other's nesting state.
CALL_TOOL_DEPTH_CAP = 4
_call_tool_state = threading.local()


def _call_tool_depth() -> int:
    return int(getattr(_call_tool_state, "depth", 0))


# ---- Public types ----------------------------------------------


@dataclass(frozen=True)
class Manifest:
    """Validated `manifest.toml` contents."""

    name: str
    version: str
    description: str
    api_version: str
    provides_tools: tuple[str, ...]
    provides_prompt_sections: tuple[str, ...]
    provides_providers: tuple[str, ...]
    requires_python: str
    requires_env: tuple[str, ...]
    requires_binaries: tuple[str, ...]
    in_subagents: bool
    source: Path  # absolute path to manifest.toml


@dataclass(frozen=True)
class ToolHookResult:
    """Return value from a v2 ``before_tool`` hook.

    Returning ``None`` (or omitting all fields → ``decision="allow"``)
    is a no-op observer — the call proceeds with the original args and
    no message is injected.

    Fields:
      - ``decision``: one of ``"allow"`` / ``"block"`` / ``"mutate"``.
        ``"block"`` short-circuits the call before it reaches
        ``_execute_tool`` (and before any permission prompt). The model
        sees a synthetic ``<blocked by plugin <name>: <reason>>`` tool
        result.
        ``"mutate"`` runs the tool with ``mutated_args`` instead of the
        original args. Multiple mutating plugins compose in
        registration order — each later plugin sees the args the
        earlier one returned.
      - ``reason``: required when ``decision="block"``. Surfaces in the
        synthetic tool-result string and in the structured INFO log.
      - ``mutated_args``: required when ``decision="mutate"``. Replaces
        the args dict for downstream hooks and tool execution.
      - ``extra_user_message``: optional. If non-empty, the harness
        prepends a user-role message tagged
        ``[plugin <name> notes]: <text>`` onto the next assistant turn
        (via the same ``pending_async_replies`` queue the subagent
        notes machinery uses). Combinable with any ``decision``.

    v1 plugins' return values are ignored unconditionally — the
    dispatch loop checks ``record.api_version == "2"`` before honoring
    these semantics.
    """

    decision: Literal["allow", "block", "mutate"] = "allow"
    reason: str = ""
    mutated_args: dict | None = None
    extra_user_message: str = ""


@dataclass(frozen=True)
class AfterToolHookResult:
    """Return value from a v2 ``after_tool`` hook.

    Both fields are optional. Returning ``None`` is a no-op observer.

    Fields:
      - ``replace_result``: if not ``None``, replaces the tool result
        string that the model sees. Multiple plugins chain in
        registration order — each later plugin sees the *replaced*
        result. Useful for secret redaction or huge-log summarisation.
        ``None`` (default) means "no replacement". Tool results are
        strings by contract; non-string replacements are not allowed
        (the dispatch loop drops them with a warning).
      - ``extra_user_message``: same shape as in ``ToolHookResult`` —
        prepended to the next assistant turn as a user-role message
        tagged with the plugin name.
    """

    extra_user_message: str = ""
    replace_result: str | None = None


@dataclass
class BeforeToolDispatch:
    """Aggregated outcome of running every plugin's ``before_tool``
    hook for one tool call. Returned to the agent's ``_route_tool``.

    - ``args``: the args dict to pass to the tool. Equals the original
      dict when no plugin mutated; otherwise the last mutator's output.
    - ``blocked`` + ``block_plugin`` + ``block_reason``: when ``blocked``,
      the agent must not invoke the tool. The synthetic
      ``<blocked by plugin <name>: <reason>>`` marker becomes the tool
      result, and an INFO log line is emitted.
    - ``mutated``: True iff at least one plugin returned
      ``decision="mutate"``. Informational only — callers usually only
      need ``args``.
    - ``extra_user_messages``: pre-formatted user-role strings to push
      onto the agent's ``pending_async_replies`` queue so they show up
      at the start of the next turn.
    """

    args: dict
    blocked: bool = False
    block_plugin: str = ""
    block_reason: str = ""
    mutated: bool = False
    extra_user_messages: list[str] = field(default_factory=list)


@dataclass
class AfterToolDispatch:
    """Aggregated outcome of running every plugin's ``after_tool`` hook
    for one tool call.

    - ``result``: the (possibly replaced) result the model should see.
      Always a string.
    - ``replaced``: True iff at least one plugin returned a non-None
      ``replace_result``.
    - ``extra_user_messages``: same shape as on ``BeforeToolDispatch``.
    """

    result: str
    replaced: bool = False
    extra_user_messages: list[str] = field(default_factory=list)


def _format_plugin_note(plugin_name: str, text: str) -> str:
    """Format a plugin-contributed extra_user_message into the
    canonical user-role string the agent sees on the next turn.

    Mirrors the ``[subagent <name> (<id>) reports]: <text>`` shape
    used by ``pending_async_replies`` for async-subagent notes so the
    LLM has one consistent grammar for "harness-injected note from
    component X".
    """
    return f"[plugin {plugin_name} notes]: {text}"


@dataclass(frozen=True)
class Message:
    """Read-only view of one conversation turn.

    `text` is the user's typed input or the assistant's response text.
    Empty string for tool-result turns and for assistant turns that
    contained only tool calls.
    """

    role: str  # "user" | "assistant"
    text: str


@dataclass
class PromptContext:
    """Read-only context passed to prompt-section renderers.

    `recent_messages` is a sliced view of the last few conversation
    turns the agent has accumulated, normalized into `Message`
    objects so plugins don't have to know pyagent's internal dict
    shape.
    """

    recent_messages: tuple[Message, ...] = ()


@dataclass
class _RegisteredSection:
    name: str
    renderer: Callable[[PromptContext], str]
    volatile: bool
    plugin_name: str


@dataclass(frozen=True)
class _RegisteredProvider:
    """One plugin-contributed LLM provider, ready to be turned into a
    `pyagent.llms.ProviderSpec` once load() completes."""

    name: str
    factory: Callable[..., Any]
    default_model: str
    env_vars: tuple[str, ...]
    plugin_name: str
    list_models: Callable[[], list[Any]] | None = None


@dataclass
class _PluginState:
    """Per-plugin registration state. Mutated only during register()."""

    manifest: Manifest
    tools: dict[str, Callable[..., Any]] = field(default_factory=dict)
    sections: list[_RegisteredSection] = field(default_factory=list)
    providers: dict[str, _RegisteredProvider] = field(default_factory=dict)
    on_start_hooks: list[Callable] = field(default_factory=list)
    on_end_hooks: list[Callable] = field(default_factory=list)
    after_response_hooks: list[Callable] = field(default_factory=list)
    before_tool_hooks: list[Callable] = field(default_factory=list)
    after_tool_hooks: list[Callable] = field(default_factory=list)


class PluginAPI:
    """The single seam between plugin code and pyagent internals.

    Exposed to plugins via their `register(api)` entrypoint.
    Read-only attributes give the plugin its scoped state directories
    and config; registration methods record what the plugin
    contributes.
    """

    def __init__(
        self,
        plugin_state: _PluginState,
        loader: "LoadedPlugins | None" = None,
    ) -> None:
        self._state = plugin_state
        # Back-reference to the LoadedPlugins instance so
        # `write_session_attachment` can find the active session that
        # the agent binds via `LoadedPlugins.bind_session()` after
        # session construction.
        self._loader = loader
        self._frozen = False

    # ---- read-only attributes -----------------------------------

    @property
    def config_dir(self) -> Path:
        return paths.config_dir()

    @property
    def workspace(self) -> Path:
        return Path.cwd().resolve()

    @property
    def user_data_dir(self) -> Path:
        d = paths.data_dir() / "plugins" / self._state.manifest.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def plugin_config(self) -> dict:
        cfg = config.load()
        plugins_table = cfg.get("plugins", {})
        if not isinstance(plugins_table, dict):
            return {}
        ours = plugins_table.get(self._state.manifest.name, {})
        return ours if isinstance(ours, dict) else {}

    @property
    def plugin_name(self) -> str:
        return self._state.manifest.name

    # ---- session-scoped writes ----------------------------------

    def write_session_attachment(
        self,
        tool_name: str,
        content: str | bytes,
        suffix: str = "",
    ) -> Path | None:
        """Write to the current session's attachments dir.

        Returns ``None`` if no session is active (e.g. the bench
        harness or a no-session test run). Plugins should fall back
        gracefully — render inline-only when the path is ``None``.

        Most plugins should prefer returning
        ``Attachment(content=..., inline_text=..., suffix=...)`` from
        a tool — the agent's render path writes the file *and* glues
        the inline rendering with the ``[also saved: <path>]`` footer
        in one shot. Use this method directly when the plugin wants
        explicit control over file layout (e.g. multiple files per
        call) or wants to write before constructing the inline
        rendering.
        """
        loader = self._loader
        if loader is None:
            return None
        session = loader.session
        if session is None:
            return None
        return session.write_attachment(tool_name, content, suffix)

    # ---- cross-plugin tool composition --------------------------

    def call_tool(self, name: str, **kwargs: Any) -> str:
        """Invoke another registered tool from inside a tool body.

        Returns the called tool's raw output (string convention with
        ``<… error: …>`` markers — propagated as-is). All failure
        modes return string markers; this method does not raise into
        the caller's tool body.

        Resolution: when the agent is bound (production), the agent's
        effective tool registry is the source — that's the registry
        post-role-allowlist filtering, so a role-restricted subagent's
        ``role_tools`` constraint applies through composition the same
        way it applies to direct LLM-issued calls. When the agent is
        *not* bound (test fixtures driving PluginAPI directly), we
        fall back to the plugin loader's resolved registry; this only
        matters in unit tests.

        Failure markers:

        - ``<error: tool {name!r} not available in this context>`` when
          the tool isn't in the effective registry. Causes: subagents
          where the providing plugin is ``in_subagents = false``;
          role-restricted subagents whose ``role_tools`` excludes the
          tool; plugin failed to load; test harness with no loader.
        - ``<error: tool composition depth exceeded>`` when a chain of
          ``call_tool`` invocations would exceed ``CALL_TOOL_DEPTH_CAP``.
          Bounds A → B → A loops without forbidding all recursion.
        - ``<error: tool {name!r} raised: <ExcType>: <msg>>`` when the
          called tool body raised. The agent-loop's ``_route_tool``
          uses a similar shape; ``call_tool`` mirrors it so plugin
          authors don't need a try/except around every composition.
        - ``<error: name must be a non-empty string …>`` for bad input.

        Permissions: the called tool inherits the calling agent's
        permission scope. ``permissions.require_access(...)`` is
        module-global (set once at process boot), so file-access
        prompts for the called tool surface through the same handler
        the calling tool would have used. There is no separate
        "system" context.

        Discoverability: ``call_tool`` is a plugin-author API only —
        it is NOT exposed to the LLM through the tool list, and
        plugins must not re-publish wrappers that re-expose it as an
        agent-facing tool.

        Available since pyagent runtime that ships ``api_version = "2"``
        plumbing — older runtimes will ``AttributeError`` if a plugin
        invokes it. No manifest-version bump required: this is a
        backwards-compatible addition to the existing v2 API surface.
        """
        if not isinstance(name, str) or not name.strip():
            return (
                f"<error: name must be a non-empty string, got "
                f"{type(name).__name__}: {name!r}>"
            )
        loader = self._loader
        # Resolve fn from the most-restrictive registry available.
        # Production: agent bound → agent.tools (post-allowlist).
        # Tests: no agent bound → plugin loader registry. Bench
        # harness with neither: the not-available marker.
        fn: Callable | None = None
        if loader is not None and loader.agent is not None:
            fn = loader.agent.tools.get(name)
        elif loader is not None:
            entry = loader.tools().get(name)
            if entry is not None:
                _, fn = entry
        if fn is None:
            return (
                f"<error: tool {name!r} not available in this context>"
            )
        depth = _call_tool_depth()
        if depth >= CALL_TOOL_DEPTH_CAP:
            return "<error: tool composition depth exceeded>"
        _call_tool_state.depth = depth + 1
        try:
            return fn(**kwargs)
        except Exception as e:  # noqa: BLE001 — surface as marker
            return (
                f"<error: tool {name!r} raised: "
                f"{type(e).__name__}: {e}>"
            )
        finally:
            _call_tool_state.depth = depth

    # ---- registration -------------------------------------------

    def _check_open(self, what: str) -> None:
        if self._frozen:
            raise RuntimeError(
                f"{what} called after register() returned; "
                "registration must happen synchronously inside register()"
            )

    def register_tool(self, name: str, fn: Callable) -> None:
        """Register a Python function as an LLM tool."""
        self._check_open("register_tool")
        if name in self._state.tools:
            raise ValueError(
                f"plugin {self._state.manifest.name!r} already registered "
                f"tool {name!r} during this register() call"
            )
        self._state.tools[name] = fn

    def register_provider(
        self,
        name: str,
        factory: Callable[..., Any],
        *,
        default_model: str = "",
        env_vars: tuple[str, ...] = (),
        list_models: Callable[[], list[Any]] | None = None,
    ) -> None:
        """Register an LLM provider exposed as `<name>/<model>` for `--model`.

        `factory` matches the built-in `pyagent.llms.ProviderSpec.factory`
        signature: takes an optional `model=` kwarg, returns an object
        implementing the `LLMClient` protocol. Heavy SDK imports
        belong inside the factory so unused providers don't pay the
        import cost.

        `default_model` is the concrete model string the factory
        receives when the user passes just `<name>` with no `/<model>`
        suffix. `env_vars` is informational at this level — plugins
        should still gate their own loading on env presence via the
        manifest's `[requires] env`.

        `list_models` is an optional callable returning a list of
        `pyagent.llms.ModelInfo` records — name plus optional
        capability tags like ``"tools"`` / ``"vision"`` /
        ``"embedding"``. Used by `pyagent --list-models`. Live
        providers (e.g. a server query) may raise to signal an
        unreachable backend; the CLI catches per-provider so one bad
        source doesn't kill the whole listing.

        Conflicts with built-in providers raise immediately so the
        problem surfaces at plugin load time rather than at the next
        `--model` invocation.
        """
        self._check_open("register_provider")
        if name in self._state.providers:
            raise ValueError(
                f"plugin {self._state.manifest.name!r} already registered "
                f"provider {name!r} during this register() call"
            )
        # Deferred import: pyagent.llms imports nothing from plugins,
        # but plugins shouldn't pull in llms at module-import time —
        # this keeps the dependency one-way and load-order tolerant.
        from pyagent import llms as _llms

        for core in _llms.PROVIDERS:
            if core.name == name:
                raise ValueError(
                    f"plugin {self._state.manifest.name!r}: provider "
                    f"name {name!r} conflicts with built-in provider"
                )
        self._state.providers[name] = _RegisteredProvider(
            name=name,
            factory=factory,
            default_model=default_model,
            env_vars=tuple(env_vars),
            plugin_name=self._state.manifest.name,
            list_models=list_models,
        )

    def register_prompt_section(
        self,
        name: str,
        renderer: Callable[[PromptContext], str],
        *,
        volatile: bool = False,
    ) -> None:
        """Register a function whose return value gets injected into the
        system prompt.

        `name` must be unique across all plugins (matches `[provides]
        prompt_sections`). `volatile=True` places the section after the
        last cache_control marker so its content can change turn-to-turn
        without invalidating the cached system block.
        """
        self._check_open("register_prompt_section")
        if any(s.name == name for s in self._state.sections):
            raise ValueError(
                f"plugin {self._state.manifest.name!r} already registered "
                f"prompt section {name!r} during this register() call"
            )
        self._state.sections.append(
            _RegisteredSection(
                name=name,
                renderer=renderer,
                volatile=volatile,
                plugin_name=self._state.manifest.name,
            )
        )

    # ---- lifecycle hooks ----------------------------------------

    def on_session_start(self, fn: Callable[[Any], None]) -> None:
        self._check_open("on_session_start")
        self._state.on_start_hooks.append(fn)

    def on_session_end(self, fn: Callable[[Any], None]) -> None:
        self._check_open("on_session_end")
        self._state.on_end_hooks.append(fn)

    def after_assistant_response(self, fn: Callable[[str], None]) -> None:
        self._check_open("after_assistant_response")
        self._state.after_response_hooks.append(fn)

    def before_tool_call(self, fn: Callable[[str, dict], None]) -> None:
        self._check_open("before_tool_call")
        self._state.before_tool_hooks.append(fn)

    def after_tool_call(
        self, fn: Callable[[str, dict, str], None]
    ) -> None:
        self._check_open("after_tool_call")
        self._state.after_tool_hooks.append(fn)

    # ---- utility -----------------------------------------------

    def log(self, level: str, message: str) -> None:
        """Emit a structured log line tagged with the plugin name."""
        method = getattr(logger, level, None)
        if not callable(method):
            method = logger.info
        method("[%s] %s", self._state.manifest.name, message)


# ---- Manifest parsing ------------------------------------------


def _parse_manifest(manifest_path: Path) -> Manifest | None:
    """Parse and validate manifest.toml. Returns None on failure."""
    try:
        with manifest_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(
            "plugin manifest %s unreadable: %s", manifest_path, e
        )
        return None

    required = ("name", "version", "description", "api_version")
    missing = [k for k in required if not data.get(k)]
    if missing:
        logger.warning(
            "plugin manifest %s missing required field(s): %s",
            manifest_path,
            ", ".join(missing),
        )
        return None

    if str(data.get("api_version")) not in SUPPORTED_API_VERSIONS:
        logger.warning(
            "plugin %s: api_version %r unsupported (supported: %s); skipping",
            data.get("name"),
            data.get("api_version"),
            sorted(SUPPORTED_API_VERSIONS),
        )
        return None

    provides = data.get("provides", {})
    if not isinstance(provides, dict):
        logger.warning(
            "plugin %s: [provides] is not a table", data.get("name")
        )
        return None

    requires = data.get("requires", {}) or {}
    if not isinstance(requires, dict):
        requires = {}

    load_table = data.get("load", {}) or {}
    if not isinstance(load_table, dict):
        load_table = {}

    return Manifest(
        name=str(data["name"]),
        version=str(data["version"]),
        description=str(data["description"]),
        api_version=str(data["api_version"]),
        provides_tools=tuple(
            str(t) for t in (provides.get("tools") or [])
        ),
        provides_prompt_sections=tuple(
            str(s) for s in (provides.get("prompt_sections") or [])
        ),
        provides_providers=tuple(
            str(p) for p in (provides.get("providers") or [])
        ),
        requires_python=str(requires.get("python") or ""),
        requires_env=tuple(
            str(v) for v in (requires.get("env") or [])
        ),
        requires_binaries=tuple(
            str(b) for b in (requires.get("binaries") or [])
        ),
        in_subagents=bool(load_table.get("in_subagents", True)),
        source=manifest_path.resolve(),
    )


def _eligibility_check(manifest: Manifest) -> str | None:
    """Return None if the plugin can run, else a reason string."""
    for var in manifest.requires_env:
        if not os.environ.get(var):
            return f"required env var {var!r} not set"
    for binary in manifest.requires_binaries:
        if shutil.which(binary) is None:
            return f"required binary {binary!r} not found on PATH"
    return None


# ---- Discovery -------------------------------------------------


@dataclass
class PluginRecord:
    """One discovered plugin, before it has been loaded."""

    manifest: Manifest
    tier: str  # "bundled" | "entry_point" | "user" | "project"
    plugin_dir: Path | None
    entry_point: Any = None  # importlib.metadata.EntryPoint, or None
    shadowed_by: list[Path] = field(default_factory=list)
    # True unless the plugin is explicitly disabled (via
    # `[plugins.<name>] enabled = false` for entry-point/drop-in
    # plugins, or omitted from `built_in_plugins_enabled` for
    # bundled). Disabled plugins still appear in discover() so the
    # rich missing-tool error can cite them; load() skips them.
    enabled: bool = True


def _scan_dir(root: Path, tier: str) -> list[PluginRecord]:
    """Walk one tier root for plugin directories."""
    if not root.exists():
        return []
    records: list[PluginRecord] = []
    for plugin_dir in sorted(root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        manifest_path = plugin_dir / "manifest.toml"
        if not manifest_path.exists():
            continue
        manifest = _parse_manifest(manifest_path)
        if manifest is None:
            continue
        records.append(
            PluginRecord(
                manifest=manifest,
                tier=tier,
                plugin_dir=plugin_dir.resolve(),
            )
        )
    return records


def _scan_entry_points() -> list[PluginRecord]:
    """Discover plugins declared via [project.entry-points."pyagent.plugins"]."""
    records: list[PluginRecord] = []
    try:
        entries = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return []
    for entry in entries:
        try:
            spec = importlib.util.find_spec(entry.module)
            if spec is None:
                logger.warning(
                    "entry point %s: cannot locate module %s",
                    entry.name,
                    entry.module,
                )
                continue
            search_locs = spec.submodule_search_locations
            if not search_locs:
                logger.warning(
                    "entry point %s: %s is not a package",
                    entry.name,
                    entry.module,
                )
                continue
            pkg_dir = Path(list(search_locs)[0])
            manifest_path = pkg_dir / "manifest.toml"
            if not manifest_path.exists():
                logger.warning(
                    "entry point %s: no manifest.toml in %s",
                    entry.name,
                    pkg_dir,
                )
                continue
            manifest = _parse_manifest(manifest_path)
            if manifest is None:
                continue
            records.append(
                PluginRecord(
                    manifest=manifest,
                    tier="entry_point",
                    plugin_dir=pkg_dir.resolve(),
                    entry_point=entry,
                )
            )
        except Exception as e:
            logger.warning(
                "entry point %s discovery failed: %s", entry.name, e
            )
    return records


def _bundled_root() -> Path:
    return Path(str(resources.files(PACKAGE_PLUGINS_PKG)))


def _enabled_bundled_names() -> set[str]:
    cfg = config.load()
    raw = cfg.get("built_in_plugins_enabled", [])
    if not isinstance(raw, list):
        logger.warning(
            "config.built_in_plugins_enabled is not a list; ignoring"
        )
        return set()
    return {n for n in raw if isinstance(n, str)}


def _is_disabled_in_config(plugin_name: str) -> bool:
    cfg = config.load()
    plugins_table = cfg.get("plugins", {})
    if not isinstance(plugins_table, dict):
        return False
    plugin_cfg = plugins_table.get(plugin_name, {})
    if not isinstance(plugin_cfg, dict):
        return False
    return plugin_cfg.get("enabled") is False


def discover() -> list[PluginRecord]:
    """Discover all plugins across all tiers, applying tier precedence
    and config gating. Returns records in load order.

    Tier precedence: project > user > entry_point > bundled. Within a
    tier, sorted by directory name (or plugin name for entry points).
    Bundled plugins are filtered against `built_in_plugins_enabled`;
    plugins explicitly disabled via `[plugins.<name>] enabled = false`
    are excluded.
    """
    bundled_root = None
    try:
        bundled_root = _bundled_root()
    except (ModuleNotFoundError, FileNotFoundError):
        # No bundled plugins package yet (Stage 1 ships before any
        # bundled plugins exist).
        bundled_root = None
    bundled = _scan_dir(bundled_root, tier="bundled") if bundled_root else []
    enabled_bundled = _enabled_bundled_names()
    # Mark bundled plugins NOT in built_in_plugins_enabled as disabled
    # rather than dropping them — the rich missing-tool error needs
    # to know they exist.
    for r in bundled:
        if r.manifest.name not in enabled_bundled:
            r.enabled = False

    entry_points = _scan_entry_points()
    user = _scan_dir(paths.config_dir() / "plugins", tier="user")
    project = _scan_dir(LOCAL_PLUGINS_DIR, tier="project")

    by_name: dict[str, PluginRecord] = {}
    shadowed: dict[str, list[Path]] = {}

    # Iterate lowest precedence first; later tiers replace.
    for record in bundled + entry_points + user + project:
        existing = by_name.get(record.manifest.name)
        if existing and existing.plugin_dir is not None:
            shadowed.setdefault(record.manifest.name, []).append(
                existing.plugin_dir
            )
        by_name[record.manifest.name] = record

    final: list[PluginRecord] = []
    for name, record in by_name.items():
        if _is_disabled_in_config(name):
            logger.info("plugin %s: disabled in config", name)
            record.enabled = False
        record.shadowed_by = shadowed.get(name, [])
        final.append(record)

    def _sort_key(r: PluginRecord) -> tuple[int, str]:
        tier_order = {"bundled": 0, "entry_point": 1, "user": 2, "project": 3}
        if r.plugin_dir is not None:
            return (tier_order.get(r.tier, 99), r.plugin_dir.name)
        return (tier_order.get(r.tier, 99), r.manifest.name)

    final.sort(key=_sort_key)
    return final


# ---- Loading ---------------------------------------------------


def _load_module(record: PluginRecord) -> Any | None:
    """Import the plugin's Python entrypoint and return the module
    object that exposes `register`."""
    if record.entry_point is not None:
        try:
            return importlib.import_module(record.entry_point.module)
        except Exception as e:
            logger.warning(
                "plugin %s: import failed: %s",
                record.manifest.name,
                e,
            )
            return None

    if record.plugin_dir is None:
        return None

    plugin_py = record.plugin_dir / "plugin.py"
    if plugin_py.exists():
        synth_name = (
            f"pyagent_plugin_{record.manifest.name.replace('-', '_')}"
        )
        # Detect synth-name collision (e.g. "memory-vector" and
        # "memory_vector" both map to pyagent_plugin_memory_vector).
        # Skip the second to avoid silently overwriting sys.modules
        # and corrupting the first plugin's relative imports.
        if synth_name in sys.modules:
            logger.warning(
                "plugin %s: synthetic module name %r already taken "
                "(probably a name collision after dash/underscore "
                "normalization); skipping plugin",
                record.manifest.name,
                synth_name,
            )
            return None
        spec = importlib.util.spec_from_file_location(
            synth_name,
            plugin_py,
            submodule_search_locations=[str(record.plugin_dir)],
        )
        if spec is None or spec.loader is None:
            logger.warning(
                "plugin %s: cannot create import spec for %s",
                record.manifest.name,
                plugin_py,
            )
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[synth_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            logger.warning(
                "plugin %s: import failed: %s",
                record.manifest.name,
                e,
            )
            sys.modules.pop(synth_name, None)
            return None
        return module

    # Bundled plugin laid out as a real Python package under
    # pyagent.plugins.<name>; import normally.
    pkg_name = (
        f"{PACKAGE_PLUGINS_PKG}."
        f"{record.manifest.name.replace('-', '_')}"
    )
    try:
        return importlib.import_module(pkg_name)
    except Exception as e:
        logger.warning(
            "plugin %s: import failed: %s", record.manifest.name, e
        )
        return None


def _validate_provides(state: _PluginState) -> str | None:
    """Check that registered names match `[provides]`. Returns a
    diagnostic string if there's a mismatch, else None."""
    declared_tools = set(state.manifest.provides_tools)
    actual_tools = set(state.tools)
    declared_sections = set(state.manifest.provides_prompt_sections)
    actual_sections = {s.name for s in state.sections}
    declared_providers = set(state.manifest.provides_providers)
    actual_providers = set(state.providers)

    problems: list[str] = []
    missing_tools = declared_tools - actual_tools
    extra_tools = actual_tools - declared_tools
    missing_sections = declared_sections - actual_sections
    extra_sections = actual_sections - declared_sections
    missing_providers = declared_providers - actual_providers
    extra_providers = actual_providers - declared_providers
    if missing_tools:
        problems.append(
            f"tools declared but not registered: {sorted(missing_tools)}"
        )
    if extra_tools:
        problems.append(
            f"tools registered but not declared: {sorted(extra_tools)}"
        )
    if missing_sections:
        problems.append(
            f"prompt_sections declared but not registered: "
            f"{sorted(missing_sections)}"
        )
    if extra_sections:
        problems.append(
            f"prompt_sections registered but not declared: "
            f"{sorted(extra_sections)}"
        )
    if missing_providers:
        problems.append(
            f"providers declared but not registered: "
            f"{sorted(missing_providers)}"
        )
    if extra_providers:
        problems.append(
            f"providers registered but not declared: "
            f"{sorted(extra_providers)}"
        )
    return "; ".join(problems) if problems else None


@dataclass
class LoadedPlugins:
    """Aggregated state across all successfully-loaded plugins.

    The agent uses this to enumerate registered tools, prompt-section
    renderers, and lifecycle hooks. Built once at agent bootstrap;
    immutable thereafter for the agent process lifetime.
    """

    states: list[_PluginState] = field(default_factory=list)
    shadowed: dict[str, list[Path]] = field(default_factory=dict)
    # Maps tool_name -> plugin_name across ALL discovered plugins
    # (including disabled ones), so the rich missing-tool error can
    # cite an installed-but-disabled plugin.
    declared_tool_provenance: dict[str, str] = field(default_factory=dict)
    # Whether this loader was built for a subagent process. Recorded
    # at load() time so `rescan_for_new` can apply the same
    # `in_subagents = false` filter that `load()` did, without
    # reaching back to the call site.
    is_subagent: bool = False
    # Effective (after-conflict-resolution) tool registry; populated
    # by `_resolve_conflicts` at end of load(). Plugin-private — not
    # exposed mutably; consumers use tools() / sections().
    _resolved_tools: dict[str, tuple[str, Callable]] = field(default_factory=dict)
    _resolved_sections: list[_RegisteredSection] = field(default_factory=list)
    _resolved_providers: dict[str, _RegisteredProvider] = field(
        default_factory=dict
    )
    # Active session for plugin-side writes via
    # `PluginAPI.write_session_attachment`. The agent's bootstrap
    # constructs the session after `load()` returns, then calls
    # `bind_session(session)` to populate this. Stays `None` in
    # bench / no-session contexts; plugins fall back to inline-only.
    session: Any | None = None
    # Active agent for `PluginAPI.call_tool` resolution. When set,
    # `call_tool` looks up tool names in `agent.tools` (the effective
    # registry post-role-allowlist filtering and post-conflict
    # resolution) rather than the plugin-only registry. This makes
    # role_tools constraints apply through composition the same way
    # they apply to direct LLM-issued calls. Stays `None` in test
    # fixtures driving PluginAPI directly without a real Agent;
    # `call_tool` then falls back to the plugin registry.
    agent: Any | None = None

    def bind_session(self, session: Any | None) -> None:
        """Attach the active Session so `PluginAPI.write_session_attachment`
        can resolve a path. Called once by the agent bootstrap after
        load() returns and the session is constructed; stays unset in
        contexts that don't have a session (the bench harness, certain
        test fixtures)."""
        self.session = session

    def bind_agent(self, agent: Any | None) -> None:
        """Attach the active Agent so `PluginAPI.call_tool` resolves
        through the agent's effective tool registry (post-role-
        allowlist) rather than the plugin-only registry. Called once
        by the agent bootstrap after the Agent is constructed and all
        tools are registered (built-ins + plugin tools). Stays unset
        in test fixtures that don't construct a real Agent."""
        self.agent = agent

    def tools(self) -> Mapping[str, tuple[str, Callable]]:
        """Effective tool name → (plugin_name, fn) after conflict
        resolution. First plugin to register wins; later duplicates
        skipped. Returns an immutable view; mutating would corrupt
        agent state."""
        return MappingProxyType(self._resolved_tools)

    def sections(self) -> tuple[_RegisteredSection, ...]:
        """All effective prompt sections, in registration order.
        Returns an immutable tuple."""
        return tuple(self._resolved_sections)

    def providers(self) -> Mapping[str, _RegisteredProvider]:
        """Effective provider name → registration record after conflict
        resolution. First plugin to register wins."""
        return MappingProxyType(self._resolved_providers)

    def _resolve_conflicts(self) -> None:
        """Walk plugin states in load order; first registration wins
        for tools, prompt sections, and providers. Later duplicates
        emit a warn log and are excluded from the effective registries.
        """
        seen_tools: set[str] = set()
        seen_sections: set[str] = set()
        seen_providers: set[str] = set()
        for state in self.states:
            for tool_name, fn in state.tools.items():
                if tool_name in seen_tools:
                    logger.warning(
                        "tool %r already registered by an earlier plugin; "
                        "%s's registration skipped",
                        tool_name,
                        state.manifest.name,
                    )
                    continue
                seen_tools.add(tool_name)
                self._resolved_tools[tool_name] = (
                    state.manifest.name,
                    fn,
                )
            for section in state.sections:
                if section.name in seen_sections:
                    logger.warning(
                        "prompt section %r already registered by an "
                        "earlier plugin; %s's registration skipped",
                        section.name,
                        state.manifest.name,
                    )
                    continue
                seen_sections.add(section.name)
                self._resolved_sections.append(section)
            for prov_name, prov in state.providers.items():
                if prov_name in seen_providers:
                    logger.warning(
                        "provider %r already registered by an earlier "
                        "plugin; %s's registration skipped",
                        prov_name,
                        state.manifest.name,
                    )
                    continue
                seen_providers.add(prov_name)
                self._resolved_providers[prov_name] = prov

    def rescan_for_new(self, agent: Any) -> int:
        """Discover plugins that have appeared on disk since `load()` and
        bring them live in this running session.

        Called from the top of the agent's main run loop so a plugin
        the LLM authored (via the write-plugin skill) is callable on
        its very next API turn — no process restart and no explicit
        reload tool. Existing plugins are left untouched: this is
        "add new" only. In-place edits to a loaded plugin's source
        won't be picked up because the synthetic module is already
        cached in ``sys.modules``; full module-cache invalidation is
        intentionally out of scope.

        Per newly-discovered record, the same gates ``load()`` runs
        apply — disabled, ``in_subagents=False`` while ``self.is_subagent``,
        ``_eligibility_check`` (env vars / binaries) — so a plugin
        gated out at startup stays gated out on rescan.

        Conflicts (a new plugin claiming a tool/section/provider name
        already taken by a built-in or earlier plugin) are first-wins
        skip + log, mirroring ``_resolve_conflicts``. The injected
        loader note tells the LLM both what loaded *and* what was
        skipped, so it doesn't try to call a tool the rescan silently
        dropped.

        Cost on a no-op scan is one ``discover()`` call (four
        ``iterdir()`` walks plus a ``tomllib.load`` per manifest).
        Runs every iteration of the agent's main loop; if it ever
        shows up in profiles, gate on tier-root ``st_mtime``.

        Returns the number of plugins newly loaded by this scan; ``0``
        means steady state.
        """
        records = discover()
        existing_names = {s.manifest.name for s in self.states}

        # Refresh declared_tool_provenance so a newly-installed-but-
        # disabled plugin's tools still surface in the rich
        # missing-tool error. setdefault preserves the original
        # discoverer when names overlap.
        for r in records:
            for tool in r.manifest.provides_tools:
                self.declared_tool_provenance.setdefault(
                    tool, r.manifest.name
                )

        new_states: list[_PluginState] = []
        for record in records:
            if record.manifest.name in existing_names:
                continue
            if not record.enabled:
                continue
            if self.is_subagent and not record.manifest.in_subagents:
                logger.info(
                    "plugin %s: skipped in subagent (in_subagents=false)",
                    record.manifest.name,
                )
                continue
            reason = _eligibility_check(record.manifest)
            if reason:
                logger.info(
                    "plugin %s: skipped (%s)",
                    record.manifest.name,
                    reason,
                )
                continue
            state = _load_one_record(record, self)
            if state is None:
                continue
            new_states.append(state)
            self.states.append(state)
            if record.shadowed_by:
                self.shadowed[record.manifest.name] = record.shadowed_by

        if not new_states:
            return 0

        # Splice each new state's contributions into the resolved
        # tables and the agent's effective registry. Track per-state
        # what actually went live vs got skipped so the loader note
        # can be honest about partial registration.
        live_tools_by_plugin: dict[str, list[str]] = {}
        skipped_tools_by_plugin: dict[str, list[str]] = {}
        live_sections_by_plugin: dict[str, list[str]] = {}
        new_provider_count = 0

        # Gate on agent.tools (built-ins + already-loaded plugin tools)
        # rather than _resolved_tools alone — agent.tools is the
        # source of truth for callability and includes built-ins the
        # loader registry doesn't know about.
        for state in new_states:
            plugin_name = state.manifest.name
            live_tools: list[str] = []
            skipped_tools: list[str] = []
            for tool_name, fn in state.tools.items():
                if tool_name in agent.tools:
                    logger.warning(
                        "tool %r already registered (built-in or "
                        "earlier plugin); %s's registration skipped",
                        tool_name,
                        plugin_name,
                    )
                    skipped_tools.append(tool_name)
                    continue
                self._resolved_tools[tool_name] = (plugin_name, fn)
                agent.add_tool(tool_name, fn)
                live_tools.append(tool_name)
            live_tools_by_plugin[plugin_name] = live_tools
            skipped_tools_by_plugin[plugin_name] = skipped_tools

            live_sections: list[str] = []
            for section in state.sections:
                if any(
                    s.name == section.name
                    for s in self._resolved_sections
                ):
                    logger.warning(
                        "prompt section %r already registered by an "
                        "earlier plugin; %s's registration skipped",
                        section.name,
                        plugin_name,
                    )
                    continue
                self._resolved_sections.append(section)
                live_sections.append(section.name)
            live_sections_by_plugin[plugin_name] = live_sections

            for prov_name, prov in state.providers.items():
                if prov_name in self._resolved_providers:
                    logger.warning(
                        "provider %r already registered by an "
                        "earlier plugin; %s's registration skipped",
                        prov_name,
                        plugin_name,
                    )
                    continue
                self._resolved_providers[prov_name] = prov
                new_provider_count += 1

        if new_provider_count:
            _publish_plugin_providers(self)

        # Fire on_session_start once per new plugin against the
        # already-active session. Bench / no-session contexts skip.
        if self.session is not None:
            for state in new_states:
                for fn in state.on_start_hooks:
                    try:
                        fn(self.session)
                    except Exception:
                        logger.exception(
                            "plugin %s on_session_start raised",
                            state.manifest.name,
                        )

        # Tell the LLM what loaded. Same pending_async_replies channel
        # the subagent-notes machinery uses; the agent loop drains it
        # immediately after this rescan call so the message lands on
        # this turn's API request.
        for state in new_states:
            m = state.manifest
            live = live_tools_by_plugin.get(m.name, [])
            skipped = skipped_tools_by_plugin.get(m.name, [])
            sections = live_sections_by_plugin.get(m.name, [])
            parts = [
                f"loaded {m.name} v{m.version}",
                f"tools=[{', '.join(live) or '(none)'}]",
            ]
            if skipped:
                parts.append(
                    f"tools-skipped-conflict=[{', '.join(skipped)}]"
                )
            if sections:
                parts.append(f"sections=[{', '.join(sections)}]")
            note = _format_plugin_note(
                "plugin-loader", "; ".join(parts)
            )
            agent.pending_async_replies.put(note)

        return len(new_states)

    def call_on_session_start(
        self,
        session: Any,
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """Fire on_session_start hooks for every loaded plugin.

        If `cancel_check` is provided and returns True between
        plugins, the loop short-circuits — used by agent_proc to
        respond to user cancel during slow plugin startup.
        """
        for state in self.states:
            if cancel_check is not None and cancel_check():
                logger.info(
                    "on_session_start: cancel detected; skipping "
                    "remaining plugins"
                )
                return
            for fn in state.on_start_hooks:
                try:
                    fn(session)
                except Exception:
                    logger.exception(
                        "plugin %s on_session_start raised",
                        state.manifest.name,
                    )

    def call_on_session_end(self, session: Any) -> None:
        for state in self.states:
            for fn in state.on_end_hooks:
                try:
                    fn(session)
                except Exception:
                    logger.exception(
                        "plugin %s on_session_end raised",
                        state.manifest.name,
                    )

    def call_after_assistant_response(self, text: str) -> None:
        for state in self.states:
            for fn in state.after_response_hooks:
                try:
                    fn(text)
                except Exception:
                    logger.exception(
                        "plugin %s after_assistant_response raised",
                        state.manifest.name,
                    )

    def call_before_tool_call(
        self, name: str, args: dict
    ) -> "BeforeToolDispatch":
        """Fire every plugin's before_tool hook in registration order.

        Conflict resolution (matches the v2 contract documented in
        `docs/plugin-design.md`):

        - ``block`` short-circuits the dispatch loop. No further
          plugins fire after the first block. The block reason is
          carried back to the call site for surfacing as a synthetic
          tool result and an INFO log line. Hooks earlier in
          registration order than the blocker still run; their
          ``extra_user_message`` contributions are preserved on the
          returned dispatch.
        - ``mutate`` chains: later plugins see the args the earlier
          plugin returned. The returned ``args`` is the final mutated
          dict (or the original if no plugin mutated).
        - ``extra_user_message`` accumulates from every plugin that
          contributed one, each tagged with the originating plugin
          name. Returned as a list of pre-formatted strings the
          caller queues onto ``pending_async_replies``.

        v1 plugins' return values are ignored unconditionally — the
        loop only honors decision semantics when ``api_version == "2"``.
        Hooks that raise are caught and logged so one bad plugin
        doesn't poison the rest of the loop.
        """
        dispatch = BeforeToolDispatch(args=args)
        for state in self.states:
            is_v2 = state.manifest.api_version == "2"
            plugin_name = state.manifest.name
            for fn in state.before_tool_hooks:
                try:
                    rv = fn(name, dispatch.args)
                except Exception:
                    logger.exception(
                        "plugin %s before_tool_call raised",
                        plugin_name,
                    )
                    continue
                if not is_v2 or rv is None:
                    continue
                if not isinstance(rv, ToolHookResult):
                    logger.warning(
                        "plugin %s before_tool_call returned unexpected "
                        "type %r; ignoring",
                        plugin_name,
                        type(rv).__name__,
                    )
                    continue
                if rv.extra_user_message:
                    dispatch.extra_user_messages.append(
                        _format_plugin_note(
                            plugin_name, rv.extra_user_message
                        )
                    )
                if rv.decision == "block":
                    dispatch.blocked = True
                    dispatch.block_plugin = plugin_name
                    dispatch.block_reason = rv.reason or ""
                    return dispatch
                if rv.decision == "mutate":
                    if isinstance(rv.mutated_args, dict):
                        dispatch.args = rv.mutated_args
                        dispatch.mutated = True
                    else:
                        logger.warning(
                            "plugin %s before_tool_call returned "
                            "decision='mutate' without a dict "
                            "mutated_args; ignoring",
                            plugin_name,
                        )
        return dispatch

    def call_after_tool_call(
        self, name: str, args: dict, result: str, is_error: bool
    ) -> "AfterToolDispatch":
        """Fire every plugin's after_tool hook in registration order.

        v1 hooks accept ``(name, args, result)`` and have their return
        value ignored. v2 hooks accept ``(name, args, result, is_error)``
        and may return an ``AfterToolHookResult`` to replace the result
        or inject a user-role message.

        ``replace_result`` chains in registration order — each later
        plugin's hook is invoked with the result the previous plugin
        replaced. ``extra_user_message`` accumulates the same way as
        in ``call_before_tool_call``.

        ``is_error`` is the harness-computed failure signal (see
        ``pyagent.tools.is_error_result`` for the contract). Plugins
        no longer have to sniff for ``<...>`` markers themselves.
        """
        dispatch = AfterToolDispatch(result=result)
        for state in self.states:
            is_v2 = state.manifest.api_version == "2"
            plugin_name = state.manifest.name
            for fn in state.after_tool_hooks:
                try:
                    if is_v2:
                        rv = fn(name, args, dispatch.result, is_error)
                    else:
                        rv = fn(name, args, dispatch.result)
                except Exception:
                    logger.exception(
                        "plugin %s after_tool_call raised",
                        plugin_name,
                    )
                    continue
                if not is_v2 or rv is None:
                    continue
                if not isinstance(rv, AfterToolHookResult):
                    logger.warning(
                        "plugin %s after_tool_call returned unexpected "
                        "type %r; ignoring",
                        plugin_name,
                        type(rv).__name__,
                    )
                    continue
                if rv.extra_user_message:
                    dispatch.extra_user_messages.append(
                        _format_plugin_note(
                            plugin_name, rv.extra_user_message
                        )
                    )
                if rv.replace_result is not None:
                    if not isinstance(rv.replace_result, str):
                        logger.warning(
                            "plugin %s after_tool_call replace_result "
                            "must be str | None, got %r; ignoring",
                            plugin_name,
                            type(rv.replace_result).__name__,
                        )
                        continue
                    dispatch.result = rv.replace_result
                    dispatch.replaced = True
        return dispatch


def _load_one_record(
    record: PluginRecord, loaded: "LoadedPlugins"
) -> _PluginState | None:
    """Import one plugin's module, run its ``register()``, validate
    declared-vs-registered names, and return the resulting
    ``_PluginState``. Returns ``None`` if any step fails (logged).

    Caller is responsible for the upstream gates (``record.enabled``,
    ``in_subagents``, ``_eligibility_check``) and for splicing the
    returned state into resolved registries — this helper only handles
    the import-and-register sequence so ``load()`` and
    ``rescan_for_new()`` can share it.
    """
    module = _load_module(record)
    if module is None:
        return None
    register_fn = getattr(module, "register", None)
    if not callable(register_fn):
        logger.warning(
            "plugin %s: no register() function in plugin module",
            record.manifest.name,
        )
        return None
    state = _PluginState(manifest=record.manifest)
    api = PluginAPI(state, loader=loaded)
    try:
        register_fn(api)
    except Exception:
        logger.exception(
            "plugin %s: register() raised; skipping plugin",
            record.manifest.name,
        )
        return None
    problem = _validate_provides(state)
    if problem:
        logger.warning(
            "plugin %s: [provides] mismatch: %s; skipping plugin",
            record.manifest.name,
            problem,
        )
        return None
    api._frozen = True
    return state


def _publish_plugin_providers(loaded: "LoadedPlugins") -> None:
    """Push the loader's resolved provider table into ``pyagent.llms``
    so ``get_client("<plugin-provider>/<model>")`` can route to it.

    Called by ``load()`` once at startup and by ``rescan_for_new()``
    whenever a newly-loaded plugin contributes a provider.
    """
    from pyagent import llms as _llms

    _llms.set_plugin_providers(
        {
            name: _llms.ProviderSpec(
                name=name,
                env_vars=tuple(p.env_vars),
                default_model=p.default_model,
                factory=p.factory,
                list_models=p.list_models,
            )
            for name, p in loaded._resolved_providers.items()
        }
    )


def load(*, is_subagent: bool = False) -> LoadedPlugins:
    """Discover, validate, and import all enabled plugins.

    Returns aggregated state ready for the agent to consume. If
    `is_subagent`, plugins with `[load] in_subagents = false` are
    skipped (so naive plugins like memory-markdown only run in the
    root agent).
    """
    records = discover()

    # declared_tool_provenance covers ALL discovered plugins, including
    # disabled ones, so the rich missing-tool error can cite them.
    declared_tool_provenance: dict[str, str] = {}
    for r in records:
        for tool in r.manifest.provides_tools:
            declared_tool_provenance.setdefault(tool, r.manifest.name)

    loaded = LoadedPlugins(
        declared_tool_provenance=declared_tool_provenance,
        is_subagent=is_subagent,
    )

    for record in records:
        if not record.enabled:
            continue
        if is_subagent and not record.manifest.in_subagents:
            logger.info(
                "plugin %s: skipped in subagent (in_subagents=false)",
                record.manifest.name,
            )
            continue
        reason = _eligibility_check(record.manifest)
        if reason:
            logger.info(
                "plugin %s: skipped (%s)", record.manifest.name, reason
            )
            continue
        state = _load_one_record(record, loaded)
        if state is None:
            continue
        loaded.states.append(state)
        if record.shadowed_by:
            loaded.shadowed[record.manifest.name] = record.shadowed_by

    loaded._resolve_conflicts()

    # Publish plugin-registered providers to the LLM router so
    # `get_client("<plugin-provider>/<model>")` resolves them. The
    # router is the source of truth at call sites; the loader is the
    # only writer. Subagents call `load()` independently, so each
    # process ends up with its own narrowed view of plugin providers.
    _publish_plugin_providers(loaded)
    return loaded


# ---- Helpers used by the agent loop ----------------------------


def _to_message(entry: Any) -> Message:
    """Normalize one conversation entry into a Message.

    Handles all three shapes in pyagent's internal format:
      - {"role": "user", "content": "..."}    → Message(user, content)
      - {"role": "user", "tool_results": ...} → Message(user, "")
      - assistant turn dict                    → Message(assistant, text)
    Anything unexpected → Message("?", "")."""
    if not isinstance(entry, dict):
        return Message(role="?", text="")
    role = entry.get("role", "?")
    if role == "user":
        content = entry.get("content")
        if isinstance(content, str):
            return Message(role="user", text=content)
        return Message(role="user", text="")
    if role == "assistant":
        return Message(role="assistant", text=entry.get("text") or "")
    return Message(role=str(role), text="")


def make_prompt_context(conversation: list[Any]) -> PromptContext:
    """Build a PromptContext from the agent's conversation list.

    Slices the last RECENT_MESSAGES_WINDOW entries and wraps each in
    a frozen Message. Plugins reading `ctx.recent_messages[-1].text`
    work uniformly across user, tool-result, and assistant turns
    without knowing pyagent's internal dict shape.
    """
    tail = (
        conversation
        if len(conversation) <= RECENT_MESSAGES_WINDOW
        else conversation[-RECENT_MESSAGES_WINDOW:]
    )
    return PromptContext(
        recent_messages=tuple(_to_message(e) for e in tail)
    )


def make_list_plugins_tool(loaded: "LoadedPlugins") -> Callable[[], str]:
    """Return a tool the agent can call to introspect what plugins
    are currently loaded.

    Useful for the agent's self-improvement loop: after authoring a
    plugin and asking the user to restart, the agent calls this tool
    to confirm the plugin loaded and registered the expected surface.
    """

    def list_plugins() -> str:
        """List the plugins currently loaded into the agent.

        Returns:
            Markdown summary, one block per plugin: name, version,
            declared tools, declared prompt sections. Empty marker if
            no plugins are loaded.
        """
        if not loaded.states:
            return "(no plugins loaded)"
        lines: list[str] = []
        for state in loaded.states:
            m = state.manifest
            tool_list = ", ".join(m.provides_tools) or "(none)"
            section_list = (
                ", ".join(m.provides_prompt_sections) or "(none)"
            )
            lines.append(
                f"- **{m.name}** v{m.version}: {m.description}\n"
                f"  - tools: {tool_list}\n"
                f"  - prompt_sections: {section_list}"
            )
        return "\n".join(lines)

    return list_plugins


def format_missing_tool_error(
    name: str,
    available: list[str],
    declared_tool_provenance: dict[str, str],
) -> str:
    """Build the rich error string returned when the LLM calls a tool
    that isn't registered.

    Cites the originating plugin (from manifest `[provides]`) when the
    tool name is known to be from a disabled-but-discovered plugin.
    """
    available_str = ", ".join(sorted(available)) or "(none)"
    suggestion = ""
    plugin = declared_tool_provenance.get(name)
    if plugin:
        suggestion = (
            f"; was provided by plugin {plugin!r} "
            f"(currently disabled — enable in config.toml to restore)"
        )
    return (
        f"<tool {name!r} is not currently available; "
        f"available: {available_str}{suggestion}>"
    )
