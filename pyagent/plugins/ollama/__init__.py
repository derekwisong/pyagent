"""ollama — bundled plugin: routes through a local Ollama server.

Registers the ``ollama`` LLM provider so ``--model ollama/<name>``
goes through the local server's native ``/api/chat`` endpoint, plus
a ``list_ollama_models`` tool the agent can call to enumerate what
has been pulled locally.

Configuration (all optional, all read at register time so plugin load
never touches the network):

  - ``OLLAMA_HOST``: server URL. Plain ``host:port`` is upgraded to
    ``http://host:port``. Defaults to ``http://localhost:11434``.
  - ``OLLAMA_MODEL``: default model name. Used when the user passes
    ``--model ollama`` with no ``/<name>`` suffix. If unset, an empty
    default flows through ``resolve_model`` and the factory raises a
    clear error at call time — startup never fails just because no
    model was chosen.

Network calls are deferred to ``respond()`` and ``list_ollama_models``
so a missing or stopped server doesn't block pyagent startup or
prevent loading other providers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Capabilities we filter from the per-model tag list before showing
# them to the user. ``completion`` is reported by every chat model so
# it carries no information; ``insert`` is fill-in-the-middle
# infrastructure that pyagent doesn't surface as a feature.
_BORING_CAPABILITIES = {"completion", "insert"}


def _factory(**kw: Any):
    # Lazy import: keeps `requests` and the client class out of the
    # plugin-load critical path for users who never invoke ollama.
    from pyagent.plugins.ollama.client import OllamaClient

    model = kw.get("model") or ""
    if not model:
        raise ValueError(
            "ollama provider requires an explicit model. Pass "
            "--model ollama/<name>, or set OLLAMA_MODEL in your "
            "environment. Use `ollama list` (or call the "
            "list_ollama_models tool) to see what's installed."
        )
    return OllamaClient(model=model)


def _format_size(size: Any) -> str:
    if not isinstance(size, (int, float)) or size <= 0:
        return ""
    gb = size / (1024**3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{size / (1024**2):.0f} MB"


def _list_models():
    """Live query of `/api/tags` + per-model `/api/show` for the
    `--list-models` CLI hook.

    Lazy-imports the client so plugin load stays network-free. Raises
    on the initial `/api/tags` failure so the aggregator can render
    `(unavailable: <reason>)` — but per-model `/api/show` failures are
    swallowed: a single 404 from a borked model entry shouldn't blank
    capability data for every other model. The model still appears,
    just without capability tags.

    Capabilities come from Ollama's ``capabilities`` array (server
    0.5+); on older servers that field is absent and `ModelInfo` ends
    up with empty capabilities — same surface as a built-in provider,
    which is fine. Per-model calls run sequentially because Ollama
    tag lists are typically short and localhost-fast; if listings
    grow we can revisit with a connection pool.
    """
    from pyagent.llms import ModelInfo
    from pyagent.plugins.ollama.client import list_models, show_model

    out: list[ModelInfo] = []
    for entry in list_models():
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        caps: tuple[str, ...] = ()
        try:
            info = show_model(name)
            raw = info.get("capabilities") or []
            if isinstance(raw, list):
                caps = tuple(
                    str(c)
                    for c in raw
                    if isinstance(c, str) and c not in _BORING_CAPABILITIES
                )
        except Exception as e:
            logger.debug(
                "ollama: /api/show for %r failed (%s); listing without caps",
                name,
                e,
            )
        out.append(ModelInfo(name=name, capabilities=caps))
    return out


def register(api):
    # Snapshot the env at register time so `default_model` on the
    # ProviderSpec is stable for the agent process. Reading later
    # would mean different subagents (or the same agent after a hot
    # config change) could see different defaults — surprising.
    default_model = os.environ.get("OLLAMA_MODEL", "")

    api.register_provider(
        "ollama",
        _factory,
        default_model=default_model,
        env_vars=(),  # local server, no required env
        list_models=_list_models,
    )

    def list_ollama_models() -> str:
        """List models pulled into the local Ollama server.

        Use this when the user asks "what ollama models do I have?"
        or before suggesting ``--model ollama/<name>`` so the choice
        is one that's actually installed.

        Returns:
            Markdown bullet list — one line per model, formatted as
            ``- name (size)``. ``<no models installed>`` if the
            server is reachable but empty. ``<ollama error: ...>``
            on connection failure or non-200 response.
        """
        from pyagent.plugins.ollama.client import list_models

        try:
            models = list_models()
        except Exception as e:
            return f"<ollama error: {e}>"
        if not models:
            return "<no models installed>"
        lines: list[str] = []
        for m in models:
            name = m.get("name") or "(unnamed)"
            size_str = _format_size(m.get("size"))
            lines.append(f"- {name} ({size_str})" if size_str else f"- {name}")
        return "\n".join(lines)

    api.register_tool("list_ollama_models", list_ollama_models)
