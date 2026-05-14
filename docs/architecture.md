# Architecture

Three diagrams. See [design.md](design.md) for more detail.

## System overview

```mermaid
flowchart TB
    CLI[CLI or library code]
    AGENT["<b>Agent.run()</b><br/>turn loop"]
    PROMPT["System prompt<br/>SOUL · TOOLS · PRIMER<br/>+ plugin sections"]
    TOOLS["Tools<br/>built-in + plugin-registered"]
    PLUGINS["Plugins<br/>tools · hooks · prompt sections"]
    LLM["LLM<br/>Anthropic / OpenAI / Gemini / Ollama"]
    SESSION["Session<br/>conversation.jsonl + attachments/"]
    SUB["Subagent processes<br/>multiprocessing.spawn"]

    CLI --> AGENT
    AGENT --> PROMPT
    AGENT --> TOOLS
    AGENT --> PLUGINS
    AGENT --> LLM
    AGENT <--> SESSION
    AGENT <-->|duplex pipe| SUB
```

Notes:

- Subagents are separate OS processes, not threads. Parent and child
  talk over a duplex pipe carrying the event protocol in
  [`pyagent/protocol.py`](../pyagent/protocol.py).
- Anthropic, OpenAI, and Gemini ship as built-in clients in
  `pyagent.llms`. Ollama is added by a bundled plugin via
  `api.register_provider("ollama", ...)`. Third-party plugins can
  register providers the same way.
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
