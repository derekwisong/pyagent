# Architecture

Three diagrams. See [design.md](design.md) for more detail.

## System overview

```mermaid
flowchart TB
    APP["<b>App / CLI</b><br/>uses an Agent"]

    subgraph HARNESS["the agent harness"]
        AGENT["<b>Agent.run()</b><br/>the turn loop"]
        PROMPT["<b>Prompt Builder</b><br/>the agent's identity<br/>SOUL · TOOLS · PRIMER"]
        TOOLS["<b>Tools</b><br/>built-in + plugin-registered"]
        SKILLS["<b>Skills</b><br/>on-demand Markdown playbooks"]
        LLM["<b>LLM Providers</b><br/>Anthropic · OpenAI · Gemini · pluggable"]
        PLUGINS["<b>Plugins</b><br/>the extension seam<br/>tools · skills · providers · prompt"]

        AGENT --> PROMPT
        AGENT --> TOOLS
        AGENT --> SKILLS
        AGENT --> LLM
        AGENT --> PLUGINS
    end

    MEMORY["<b>Memory</b><br/>a subsystem shipped as a plugin"]
    RUNTIME["<b>Session + Subagents</b><br/>history · attachments · child processes"]

    APP --> AGENT
    PLUGINS --> MEMORY
    AGENT <--> RUNTIME
```

Notes:

- The "agent harness" is the framework itself: the turn loop plus the
  five things it composes — prompt builder, tools, skills, LLM
  providers, and the plugin system. An App or CLI just constructs an
  `Agent` and calls `run()`.
- Plugins are the extension seam (`PluginAPI`). They register tools and
  LLM providers, contribute system-prompt sections, and observe or
  control the turn loop. Big subsystems ship this way — memory
  (markdown ledgers + fastembed vector recall) is a bundled plugin,
  not core.
- Anthropic, OpenAI, and Gemini ship as built-in providers in
  `pyagent.llms`. Ollama is added by a bundled plugin via
  `api.register_provider("ollama", ...)`. Third-party plugins register
  providers the same way.
- Tools are callable functions the LLM invokes; skills are passive
  markdown playbooks the agent pulls in on demand (`read_skill`). Both
  come in built-in and plugin/user-provided flavors.
- "Session + subagents" is the runtime state layer: conversation
  history and attachments on disk, plus child agents spawned via
  `multiprocessing.spawn` talking over a duplex pipe carrying the event
  protocol in [`pyagent/protocol.py`](../pyagent/protocol.py).
- The permissions gate only covers the built-in filesystem and shell
  tools. Plugin tools and your own `add_tool`s don't go through it
  unless they call it themselves.

## The turn cycle

```mermaid
sequenceDiagram
    participant U as User
    participant A as Agent.run()
    participant P as Plugins
    participant L as LLM
    participant T as Tool

    U->>A: prompt
    loop until no tool_calls
        A->>L: respond(conversation, system, tools)
        L-->>A: text + optional tool_calls
        opt tool_calls
            loop each call
                A->>P: before_tool_call (may block / mutate)
                A->>T: execute
                T-->>A: result
                A->>P: after_tool_call (may replace)
            end
        end
    end
    A-->>U: final text
```

Notes:

- `before_tool_call` fires before the permissions prompt, so a plugin
  can block a call before the human is asked to approve it.
- The plugin set is rescanned at the top of every turn. A plugin the
  agent just authored (via the `write-plugin` skill) is callable on
  its next turn without restarting.

## System prompt assembly

```mermaid
flowchart LR
    subgraph CACHED["cached prefix (stable)"]
        SOUL[SOUL]
        T[TOOLS]
        PR[PRIMER]
        PLG_S[plugin sections]
    end
    BP{{breakpoint}}
    subgraph FRESH["fresh every turn"]
        VOL["volatile plugin sections<br/>· skills catalog · live state"]
    end

    SOUL --> T --> PR --> PLG_S --> BP --> VOL
```

The prefix is cached by the provider; anything past the breakpoint is
sent fresh each turn. Plugin prompt sections pick a side via
`volatile=True/False` on `register_prompt_section`. Anything that
changes turn-to-turn (memory recall, skills catalog, live checklist)
goes on the volatile side so it doesn't invalidate the cached prefix.

---

See [design.md](design.md) for more detail and
[plugin-design.md](plugin-design.md) for the plugin author API.
