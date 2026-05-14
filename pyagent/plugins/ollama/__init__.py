"""ollama — bundled plugin: routes through a local Ollama server.

Registers the ``ollama`` LLM provider so ``--model ollama/<name>``
goes through the local server's native ``/api/chat`` endpoint.
``pyagent --list-models`` from the CLI enumerates what's pulled
locally; no agent-facing tool is exposed.

Configuration (all optional, all read at register time so plugin load
never touches the network):

  - ``OLLAMA_HOST``: server URL. Plain ``host:port`` is upgraded to
    ``http://host:port``. Defaults to ``http://localhost:11434``.
  - ``OLLAMA_MODEL``: default model name. Used when the user passes
    ``--model ollama`` with no ``/<name>`` suffix. If unset, an empty
    default flows through ``resolve_model`` and the factory raises a
    clear error at call time — startup never fails just because no
    model was chosen.

Network calls are deferred to ``respond()`` and ``--list-models`` so
a missing or stopped server doesn't block pyagent startup or
prevent loading other providers.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_BORING_CAPABILITIES = {"completion", "insert"}


def _factory(**kw: Any):
    from pyagent.plugins.ollama.client import OllamaClient

    model = kw.get("model") or ""
    if not model:
        raise ValueError(
            "ollama provider requires an explicit model. Pass "
            "--model ollama/<name>, or set OLLAMA_MODEL in your "
            "environment. Run `ollama list` or "
            "`pyagent --list-models` to see what's installed."
        )
    return OllamaClient(model=model)


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
    default_model = os.environ.get("OLLAMA_MODEL", "")

    api.register_provider(
        "ollama",
        _factory,
        default_model=default_model,
        env_vars=(),
        list_models=_list_models,
    )
