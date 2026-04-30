# Plugins

Pyagent needs a plugin system where a python library can be added
to provide new functionality.

## How the plugin attaches
Plugins will register themselves with pyagent.

- register tools: plugins could bring their own tools that will get added
- register hooks
  - before_prompt_build: inject extra content into the prompt
  - before_tool_call: intercept a tool call from LLM, perhaps to validate or ask for manual approval
  - after_model_resolve: change which llm is used based on the user's intent
  - probably others...
- register provider: the plugin provides a way to register new LLMs
- register channel: (for later) bridge to other platforms (like telegram, discord, etc)

## Use case

My first initial use case is that I want to have the pyagent memory system
be served by a plugin instead of being integrated into pyenv. The memory
plugin will be bundled by default. But I would like the user to be able
to disable it outright or replace it. I don't know if I shoudl support
multiple memory plugins at once. Perhaps some classes of plugins should not
have multiple active versions. I'm not sure... this is an open question

## Separate from skills
A plugin is not a skill, but a plugin might provide some concrete behavior that a skill uses.
A plugin might provide a useful tool and hook, while the skill will teach the
llm interesting ways to use it.

