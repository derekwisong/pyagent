# Competitive Landscape — Plugin Systems in Agent Frameworks

A scan of how peer projects let users extend their agents, what their
choices imply, and what pyagent should copy or avoid. Sources are
project documentation and source as of April 2026; specifics shift —
treat as orientation, not gospel.

## TL;DR for pyagent

| Idea | Source | Adopt? |
| --- | --- | --- |
| Manifest-first metadata, validated without executing plugin code | OpenClaw | ✅ Adopt |
| Single `register(api)` entrypoint receiving a small SDK object | OpenClaw, VS Code | ✅ Adopt |
| Tiered discovery (bundled / installed / drop-in) | Skills (already in pyagent) | ✅ Extend |
| Python entry points for redistributable plugins | setuptools, MCP servers, packaging convention | ✅ Adopt |
| Subprocess-isolated plugins by default | MCP, Claude Code | ❌ Skip in v1 (overkill for in-process Python plugins) |
| Decorators that *also* import the framework as a side effect | LangChain (`@tool`) | ❌ Avoid |
| Subclassing a framework base class to "be" a plugin | early Semantic Kernel, Haystack 1.x | ❌ Avoid |
| One global registry mutated at import time | LangChain, many academic frameworks | ❌ Avoid |
| Plugin kinds with cardinality rules (singleton vs multi) | Pytest plugins, OpenClaw extension types | ✅ Adopt |
| Plugin-defined slash commands | Claude Code, OpenClaw hooks | 🟡 Defer to v2 |

## Project-by-project

### OpenClaw — closest cousin

OpenClaw (open-source self-hosted AI agent, similar SOUL/TOOLS/skills
architecture to pyagent) ships an explicit plugin SDK. Key shape:

- **Manifest-first.** Every plugin has `openclaw.plugin.json`. The
  framework validates this without executing plugin code. Malformed
  manifest = gracefully skipped.
- **`register(api)` entrypoint.** The plugin module exposes a default
  export that receives an `api` object with `registerHook`,
  `registerTool`, etc.
- **Plugin kinds.** Provider plugins, channel plugins, memory plugins,
  hook plugins are distinct concepts with different SDK surfaces.
- **Plugin SDK boundary.** Plugins import from
  `openclaw/plugin-sdk/*`; they MUST NOT import from `core` or other
  extensions. Documented and lint-enforced.
- **Eligibility rules.** Manifest declares OS/binaries/env/config
  requirements; ineligible plugins are skipped with a logged reason
  (instead of crashing on missing deps).
- **Hooks managed at plugin granularity.** You enable/disable a
  plugin, not individual hooks within it. Simpler mental model.

**What pyagent should steal:** all of the above except subscriber-
style hook IDs. The boundary between plugin and core is the most
underrated idea — every successful long-lived plugin system has it,
and most early-stage ones don't and pay for it later.

**What pyagent should not copy:** OpenClaw is TypeScript, has a
gateway architecture, supports network channels. That weight isn't
needed for a "simple but flexible" Python CLI agent.

### Model Context Protocol (MCP) — Claude Code, Cursor, et al.

Anthropic's MCP is the closest thing to a cross-framework standard for
exposing tools and resources to AI agents. Architecture:

- **Out-of-process by default.** An MCP server runs as a separate
  process; the agent (client) talks to it over JSON-RPC 2.0 (stdio or
  SSE).
- **Three primitives:** tools (callable), resources (readable),
  prompts (templated).
- **Language-agnostic.** Server can be Python, TypeScript, Go,
  whatever — protocol is the contract.
- **Discovery via config.** The host application keeps a list of MCP
  servers; each entry points at a binary or URL.

**What pyagent should steal:** the *concept* of three primitives is
clean (tools / resources / prompts maps roughly to register_tool /
register_prompt_section / [skills]). The naming is worth borrowing if
we ever expand the API surface.

**What pyagent should not copy:** the protocol overhead. MCP exists
because it bridges *across language and process boundaries*. Pyagent
plugins are Python-in-Python; calling `fn(args)` is free, and a JSON-
RPC dance buys nothing. Adding MCP server compatibility is a separate
feature ("pyagent can use MCP servers") that doesn't conflict with
having an in-process plugin API for Python plugins.

### LangChain — what to avoid

LangChain has no plugin system per se. Instead it has:

- **Massive global registries** populated by import side effects.
- **`@tool` decorators** that register on import, leading to "where
  did this tool come from?" mysteries.
- **Subclass-everything culture** (BaseTool, BaseRetriever,
  BaseChatModel) where the API surface keeps growing because you're
  inheriting from a class that itself takes on new responsibilities.
- **No clear extension boundary.** User code, framework code, and
  third-party integrations all import each other.

The result: LangChain "plugins" are actually just Python packages
that depend on `langchain-core` and shove things into module-level
state. Upgrades break extensions silently when an internal that
extensions came to depend on shifts.

**Lesson:** A plugin API that has fewer than 10 methods and a
documented "MUST NOT" list is worth more than a hundred extension
points sprayed across base classes.

### AutoGen / AG2

Multi-agent conversation framework. "Extension" = subclass an agent
class and override methods, register tools as Python functions, swap
LLMs at construction time. No manifest, no discovery, no separation
between user code and extension code. Like LangChain but smaller
surface area.

**Lesson:** "extension by subclass" works fine for prototypes and
poorly for ecosystems. If you want third parties to ship plugins users
can install, you need a manifest and a discovery mechanism. Pyagent
should have both.

### CrewAI

Agent + crew abstractions, tools as Python callables decorated with
`@tool`. No plugin system; tools are passed at construction.

**Lesson:** Same as AutoGen — fine for the scoped use case, not a
template for a plugin ecosystem.

### Goose (Block)

Goose explicitly uses MCP for extensions. An "extension" is an MCP
server — running out-of-process, talked to over JSON-RPC.

**Lesson:** Goose chose MCP because they wanted ecosystem
compatibility (any MCP server works with any MCP client). Pyagent has
no such ecosystem need yet — its plugins are Python, written for
pyagent. In-process is simpler. **But** the day pyagent adds MCP
client support (read: ability to use MCP servers as tools), it should
do so as a *bundled plugin*, not as a core feature. That keeps the
plugin API honest and gives users the ability to disable MCP
entirely.

### Semantic Kernel

Microsoft's .NET-and-Python framework calls its plugins "plugins" —
groups of "kernel functions" registered with the kernel. Modern
versions are decorator-based (`@kernel_function`) and reasonably
clean. No manifest; discovery is "you imported it."

**Lesson:** Decorator-based registration is fine *if* the registration
target is local and explicit. Semantic Kernel's `kernel.add_plugin(...)`
takes an object you constructed yourself — no global state. That's the
right pattern. Avoid "import side effect populates global registry."

### Open Interpreter

Coding agent with no plugin system; capabilities are baked in. Custom
profiles let you tweak the system prompt. That's it.

**Lesson:** Plenty of agents ship without plugins and do fine. Plugins
are for when you have a genuine extension story (memory backends,
tool packs from different domains, channels). Don't add them just
because peers have them.

### VS Code extensions (cross-domain reference)

Not an agent framework, but the most studied extension API in
software. Worth one paragraph because the pyagent design borrows ideas:

- Manifest (`package.json`) declares `contributes.*` (commands, menus,
  keybindings, languages, debuggers...). Framework reads the manifest
  to wire UI before executing extension code.
- Activation events: extensions are not loaded until a triggering
  event fires (file opened, command invoked). Lazy loading.
- A single `activate(context)` entrypoint receives a `context` object;
  the extension never imports `vscode`'s internals.
- Capability declarations and uninstall are first-class.

Pyagent v1 doesn't need lazy activation (agent startup is cheap), but
the manifest + context + boundary pattern is the right shape. v2 may
benefit from activation events (e.g. memory plugin only loads when
the agent enters a flow that uses it).

### Pytest plugins (cross-domain reference)

The pytest plugin system is a model of "small, conservative API that
lasted":

- Plugins discovered via setuptools entry points (`pytest11` group).
- Plugins implement *named* hook functions (`pytest_collection_modifyitems`,
  etc.) — the framework calls them at well-defined points.
- Hooks have a documented spec; hook implementations declare which
  spec they implement via naming convention.
- Plugin order is explicit; conflicts surface clearly.
- The API has barely changed in 15 years.

**Lesson:** Named lifecycle hooks called by the framework — not
decorators that mutate registries — age well. Pyagent's
`on_session_start` / `on_session_end` follows this shape.

## Patterns worth stealing, in priority order

1. **Manifest-first, validated without executing plugin code.** Single
   biggest win: the framework can list/disable/diagnose plugins
   without running them.
2. **Single `register(api)` entrypoint, small `api` surface.** Forces
   a clean boundary; makes the API explicit.
3. **Plugin kinds with cardinality rules.** Singleton vs multi solves
   the "two memory plugins" question without ad-hoc code.
4. **Eligibility checks in the manifest.** Makes plugins safe to ship
   to users on systems missing dependencies.
5. **Tiered discovery (bundled / pip / drop-in).** Mirrors what
   pyagent already does for skills — consistency beats novelty.
6. **Documented "MUST NOT" list.** Save your future self by writing
   down the boundary now. LangChain didn't, and ate it.

## Patterns to avoid

1. **Import-time side effects mutating global registries.** Source of
   most "where did that come from?" debugging. The plugin system must
   make registration explicit and traceable.
2. **Subclassing framework base classes as the extension model.**
   Couples plugins to internals; every base class change ripples.
3. **Open-ended hook surfaces.** "We have 40 hook points" sounds
   flexible; in practice, plugins use 4 of them and the other 36 are
   maintenance burden. Start with 4. Add more when something demands
   them.
4. **Implicit "last enabled wins" for singleton kinds.** Always fail
   loud on conflict.
5. **Mixing skills and plugins.** They're different concerns. Skills
   teach the LLM; plugins teach the framework. Keep the two systems
   aligned but separate.

## Sources

- [openclaw/openclaw on GitHub](https://github.com/openclaw/openclaw)
- [openclaw AGENTS.md](https://github.com/openclaw/openclaw/blob/main/AGENTS.md)
- [openclaw plugin docs](https://github.com/openclaw/openclaw/blob/main/docs/tools/plugin.md)
- [Model Context Protocol](https://www.anthropic.com/news/model-context-protocol)
- [Claude Code MCP integration](https://code.claude.com/docs/en/mcp)
- [pytest plugin reference](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
- [VS Code extension API overview](https://code.visualstudio.com/api)
- LangChain, AutoGen, CrewAI, Semantic Kernel, Goose, Open Interpreter — observations from project source and docs as of April 2026.
