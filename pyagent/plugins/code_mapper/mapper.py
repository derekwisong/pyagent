"""Core code-mapping logic for the code-mapper plugin.

Public surface is `map_code(path, kind, include_docstrings)` — see the
docstring on the `__init__.py` wrapper for the LLM-facing contract.
This module focuses on the parse-and-extract work and is unit-testable
without going through the plugin loader.

Design notes:

  * Per-language config lives in `queries/<lang>.toml` next to its
    `<lang>.scm` query. Adding a language is two files in `queries/`,
    no edit to this file. See `EXTENDING.md`.

  * Tree-sitter grammars are loaded lazily from
    `tree_sitter_language_pack` and cached at module level. Same for
    compiled queries. Re-parsing a file in the same agent process pays
    only the parse cost, never the grammar/query compile cost.

  * Tree-sitter is non-raising on syntax errors — bad files yield a
    tree decorated with `ERROR` / `MISSING` nodes. The mapper still
    extracts whatever symbols parsed cleanly and reports the error
    locations in the response's `errors` array.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Lazy imports of tree_sitter / tree_sitter_language_pack happen inside
# the public functions so that `import mapper` doesn't hard-fail in
# environments where the deps aren't installed yet (e.g. during
# manifest discovery).

logger = logging.getLogger(__name__)

_QUERIES_DIR = Path(__file__).parent / "queries"


# -- Per-language registry (loaded from queries/*.toml) ---------------


@dataclass
class _PromoteRule:
    src: str  # source kind to promote from
    dst: str  # promoted kind
    when_inside: tuple[str, ...]  # enclosing tree-sitter node types


@dataclass
class _LangConfig:
    language: str  # tree-sitter-language-pack id (e.g. "python")
    description: str
    extensions: tuple[str, ...]
    capture_to_kind: dict[str, str]
    definition_node_types: frozenset[str]
    promote_rules: tuple[_PromoteRule, ...]
    docstrings: str | None  # extractor name, or None
    scm_path: Path

    def kinds_emitted(self) -> set[str]:
        """All normalized kinds this language can produce, including
        rule-promoted ones. Used to populate the user-facing `kind`
        filter union."""
        out = set(self.capture_to_kind.values())
        for rule in self.promote_rules:
            out.add(rule.dst)
        return out


_REGISTRY: dict[str, _LangConfig] = {}
_EXTENSION_TO_LANG: dict[str, str] = {}
_REGISTRY_LOADED = False


def _load_registry() -> None:
    """Discover queries/*.toml and build the language registry.

    Idempotent — first call populates module-level dicts. Per-language
    failures (malformed toml, missing scm, etc.) are logged and the
    bad language is skipped; other languages still load.
    """
    global _REGISTRY_LOADED
    if _REGISTRY_LOADED:
        return
    if not _QUERIES_DIR.exists():
        _REGISTRY_LOADED = True
        return
    for toml_path in sorted(_QUERIES_DIR.glob("*.toml")):
        try:
            cfg = _parse_lang_toml(toml_path)
        except Exception as e:
            logger.warning(
                "code-mapper: bad language config %s: %s", toml_path, e
            )
            continue
        if not cfg.scm_path.exists():
            logger.warning(
                "code-mapper: %s declared but %s missing",
                toml_path.name,
                cfg.scm_path.name,
            )
            continue
        _REGISTRY[cfg.language] = cfg
        for ext in cfg.extensions:
            existing = _EXTENSION_TO_LANG.get(ext)
            if existing and existing != cfg.language:
                logger.warning(
                    "code-mapper: extension %s claimed by both %s and %s; "
                    "first wins",
                    ext,
                    existing,
                    cfg.language,
                )
                continue
            _EXTENSION_TO_LANG[ext] = cfg.language
    _REGISTRY_LOADED = True


def _parse_lang_toml(toml_path: Path) -> _LangConfig:
    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    language = str(data["language"])
    extensions = tuple(str(e).lower() for e in data["extensions"])
    captures_raw = data.get("captures", {})
    capture_to_kind = {str(k): str(v) for k, v in captures_raw.items()}
    def_types = frozenset(
        str(t) for t in data.get("definition_node_types", [])
    )
    promote_rules: list[_PromoteRule] = []
    for entry in data.get("promote", []) or []:
        promote_rules.append(
            _PromoteRule(
                src=str(entry["from"]),
                dst=str(entry["to"]),
                when_inside=tuple(
                    str(t) for t in entry.get("when_inside", [])
                ),
            )
        )
    docstrings = data.get("docstrings")
    docstrings = str(docstrings) if docstrings else None
    return _LangConfig(
        language=language,
        description=str(data.get("description", "")),
        extensions=extensions,
        capture_to_kind=capture_to_kind,
        definition_node_types=def_types,
        promote_rules=tuple(promote_rules),
        docstrings=docstrings,
        scm_path=toml_path.with_suffix(".scm"),
    )


def _all_emitted_kinds() -> set[str]:
    _load_registry()
    out: set[str] = set()
    for cfg in _REGISTRY.values():
        out |= cfg.kinds_emitted()
    return out


# Static `kind=` filters that index by category rather than by literal
# kind name. Anything not in this table is treated as a single-kind
# literal filter (e.g. kind="struct" matches symbols with kind="struct").
_NAMED_FILTERS: dict[str, set[str]] = {
    "imports": {"import"},
    "functions": {"function", "method"},
    "classes": {"class", "struct", "enum", "union", "trait", "type"},
    "constants": {"constant"},
    "calls": {"call"},
}


def _resolve_kind_filter(kind: str) -> set[str] | None:
    """Translate a user-facing `kind` argument into the set of
    normalized kinds that pass.

    `kind="all"`        → every kind any language emits, EXCEPT call.
    `kind="<named>"`    → the named filter's set (see _NAMED_FILTERS).
    `kind="<literal>"`  → that literal kind, if any language emits it.
    Unknown literal     → None (caller emits an error).
    """
    if kind == "all":
        return _all_emitted_kinds() - {"call"}
    if kind in _NAMED_FILTERS:
        return _NAMED_FILTERS[kind]
    emitted = _all_emitted_kinds()
    if kind in emitted:
        return {kind}
    return None


# -- Caches -------------------------------------------------------------

_lang_cache: dict[str, object] = {}  # language_id -> Language
_query_cache: dict[str, object] = {}  # language_id -> Query
_parser_cache: dict[str, object] = {}  # language_id -> Parser


def _get_language(lang_id: str):
    if lang_id not in _lang_cache:
        from tree_sitter_language_pack import get_language

        _lang_cache[lang_id] = get_language(lang_id)
    return _lang_cache[lang_id]


def _get_parser(lang_id: str):
    if lang_id not in _parser_cache:
        from tree_sitter import Parser

        _parser_cache[lang_id] = Parser(_get_language(lang_id))
    return _parser_cache[lang_id]


def _get_query(lang_id: str):
    if lang_id not in _query_cache:
        from tree_sitter import Query

        cfg = _REGISTRY[lang_id]
        _query_cache[lang_id] = Query(
            _get_language(lang_id), cfg.scm_path.read_text()
        )
    return _query_cache[lang_id]


# -- Symbol extraction --------------------------------------------------


@dataclass
class _Symbol:
    kind: str
    name: str
    line: int  # 1-indexed, matches read_file
    parent: str | None
    docstring: str | None = None

    def to_dict(self, include_docstrings: bool) -> dict:
        out = {
            "kind": self.kind,
            "name": self.name,
            "line": self.line,
            "parent": self.parent,
        }
        if include_docstrings and self.docstring is not None:
            out["docstring"] = self.docstring
        return out


def _enclosing_definition(
    node, def_node_types: frozenset[str]
) -> object | None:
    """Walk node.parent until we hit a node whose type is in
    `def_node_types`, or None at root. Per-language because each
    grammar names its definition nodes differently."""
    cur = node.parent
    while cur is not None:
        if cur.type in def_node_types:
            return cur
        cur = cur.parent
    return None


def _definition_name(def_node) -> str | None:
    """Pull the identifier text from the `name:` field of a definition
    node (class/function/struct/etc.). Returns None if no `name:` field
    is set — happens for unnamed/anonymous defs and for some grammar
    quirks; the caller drops `parent` to None in that case."""
    name_node = def_node.child_by_field_name("name")
    if name_node is None:
        return None
    return name_node.text.decode("utf-8", errors="replace")


def _docstring_for(def_node, source_bytes: bytes) -> str | None:
    """Return the docstring text for a class_definition or
    function_definition node, or None if absent.

    The current Python grammar (tree-sitter-python via the language
    pack) puts a docstring as a `string` node directly in the def's
    `body:` block (no expression_statement wrapper). Older grammar
    versions wrapped it; we accept both shapes.
    """
    body = def_node.child_by_field_name("body")
    if body is None:
        return None
    for child in body.children:
        if child.type == "comment":
            continue
        if child.type == "string":
            return _strip_string_quotes(
                child.text.decode("utf-8", errors="replace")
            )
        if child.type == "expression_statement":
            for sub in child.children:
                if sub.type == "string":
                    return _strip_string_quotes(
                        sub.text.decode("utf-8", errors="replace")
                    )
            return None
        # First non-trivia, non-string statement → no docstring.
        return None
    return None


def _strip_string_quotes(literal: str) -> str:
    """Best-effort: strip Python triple- or single-quoted string
    delimiters from the literal source text. Robust enough for
    docstrings; not a full Python string-literal parser."""
    s = literal.strip()
    for prefix_len in (4, 3, 2, 1):  # b"...", r"...", """...""", "..."
        if (
            len(s) > 2 * prefix_len
            and s[prefix_len:].startswith(('"""', "'''"))
            and s.endswith(s[prefix_len : prefix_len + 3])
        ):
            return s[prefix_len + 3 : -3]
    if s.startswith('"""') and s.endswith('"""'):
        return s[3:-3]
    if s.startswith("'''") and s.endswith("'''"):
        return s[3:-3]
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    return s


@dataclass
class _Match:
    """One per-pattern match from the .scm: a name node, its
    definition node, what kind the .scm tagged it as, and an optional
    @parent override from the same match."""

    cap_name: str
    name_node: object
    def_node: object
    parent_override: str | None


def _process_matches(
    matches: list[tuple[int, dict]],
) -> tuple[list[_Match], dict[int, str]]:
    """Walk QueryCursor.matches() output and produce a flat list of
    per-symbol matches plus a node→canonical-name index.

    Each match groups together the captures from a single pattern in
    the .scm — @name, @definition.X, optional @parent — so we pair
    them within the match rather than across the whole capture dict
    (cross-pattern bleed would happen when two patterns target the
    same AST node, e.g. <main id="…"> matching both @definition.section
    and @definition.element_id).

    Returns (out, def_name_index):
      - out: list of _Match records, one per emitted symbol.
      - def_name_index: maps `def_node.id` → captured name text. Used
        as the canonical name for grammars whose definition nodes lack
        a `name:` field (Rust impl_item, etc.). Populated from the
        first @name we see for each def node, so the smallest
        enclosing match wins; this is read by ancestor-walk parent
        attribution downstream.
    """
    out: list[_Match] = []
    def_name_index: dict[int, str] = {}

    for _pattern_idx, caps in matches:
        name_nodes = caps.get("name") or []
        parent_nodes = caps.get("parent") or []

        def_cap_name: str | None = None
        def_node = None
        for k, nodes in caps.items():
            if k.startswith(("definition.", "reference.")) and nodes:
                def_cap_name = k
                def_node = nodes[0]
                break
        if def_cap_name is None or def_node is None or not name_nodes:
            continue

        parent_override: str | None = None
        if parent_nodes:
            parent_override = parent_nodes[0].text.decode(
                "utf-8", errors="replace"
            )

        for nm in name_nodes:
            out.append(
                _Match(
                    cap_name=def_cap_name,
                    name_node=nm,
                    def_node=def_node,
                    parent_override=parent_override,
                )
            )
            def_name_index.setdefault(
                def_node.id, nm.text.decode("utf-8", errors="replace")
            )

    return out, def_name_index


def _collect_errors(root_node) -> list[dict]:
    """Walk the parse tree and report ERROR / MISSING nodes.

    Returns a list of `{line, message}` dicts (1-indexed lines), capped
    at a small number to keep the response bounded. The point is to
    *flag* that the file isn't fully parseable, not to be a linter.
    """
    out: list[dict] = []
    MAX = 10
    stack = [root_node]
    while stack and len(out) < MAX:
        node = stack.pop()
        if node.is_missing:
            out.append(
                {
                    "line": node.start_point[0] + 1,
                    "message": f"missing {node.type!r}",
                }
            )
        elif node.type == "ERROR":
            out.append(
                {
                    "line": node.start_point[0] + 1,
                    "message": "syntax error",
                }
            )
        # Don't recurse into ERROR subtrees — they'd flood the report.
        if node.type != "ERROR":
            stack.extend(node.children)
    return out


def _build_symbols(
    matches: list[tuple[int, dict]],
    source_bytes: bytes,
    cfg: _LangConfig,
    *,
    include_docstrings: bool,
) -> list[_Symbol]:
    processed, def_name_index = _process_matches(matches)
    symbols: list[_Symbol] = []

    for m in processed:
        kind = cfg.capture_to_kind.get(m.cap_name)
        if kind is None:
            continue
        name = m.name_node.text.decode("utf-8", errors="replace")
        parent: str | None = m.parent_override
        encl = (
            _enclosing_definition(m.def_node, cfg.definition_node_types)
            if cfg.definition_node_types
            else None
        )
        if encl is not None:
            if parent is None:
                parent = def_name_index.get(encl.id) or _definition_name(encl)
            # Apply promotion rules (e.g. function → method when
            # enclosed by class_definition). First matching rule wins.
            for rule in cfg.promote_rules:
                if kind == rule.src and encl.type in rule.when_inside:
                    kind = rule.dst
                    break
        docstring = None
        if (
            include_docstrings
            and cfg.docstrings == "python"
            and m.def_node.type
            in {"class_definition", "function_definition"}
        ):
            docstring = _docstring_for(m.def_node, source_bytes)
        symbols.append(
            _Symbol(
                kind=kind,
                name=name,
                line=m.name_node.start_point[0] + 1,
                parent=parent,
                docstring=docstring,
            )
        )

    # Stable order: by line, then by name. Makes diffs (and human
    # reads) predictable.
    symbols.sort(key=lambda s: (s.line, s.name))
    return symbols


# -- Public API --------------------------------------------------------


# Hard ceiling on emitted symbols per call. Keeps responses bounded for
# pathological files; agent can re-call with a tighter `kind` filter.
SYMBOL_LIMIT = 1000


def supported_extensions() -> tuple[str, ...]:
    _load_registry()
    return tuple(sorted(_EXTENSION_TO_LANG))


def supported_languages() -> tuple[str, ...]:
    _load_registry()
    return tuple(sorted(_REGISTRY))


def map_source(
    source_bytes: bytes,
    lang_id: str,
    *,
    kind: str = "all",
    include_docstrings: bool = False,
) -> dict:
    """Parse `source_bytes` as `lang_id` and return the symbol map dict
    (the same shape as map_code's JSON response, minus the `file`
    field). Useful for tests."""
    _load_registry()
    cfg = _REGISTRY.get(lang_id)
    if cfg is None:
        return {
            "language": lang_id,
            "symbols": [],
            "errors": [
                {
                    "line": 0,
                    "message": (
                        f"unsupported language {lang_id!r}; "
                        f"available: {list(supported_languages())}"
                    ),
                }
            ],
        }
    wanted = _resolve_kind_filter(kind)
    if wanted is None:
        return {
            "language": lang_id,
            "symbols": [],
            "errors": [
                {
                    "line": 0,
                    "message": (
                        f"unknown kind {kind!r}; valid: 'all', "
                        f"{sorted(_NAMED_FILTERS)}, or any literal "
                        f"kind emitted by a registered language: "
                        f"{sorted(_all_emitted_kinds())}"
                    ),
                }
            ],
        }

    parser = _get_parser(lang_id)
    query = _get_query(lang_id)
    tree = parser.parse(source_bytes)

    from tree_sitter import QueryCursor

    matches = list(QueryCursor(query).matches(tree.root_node))
    symbols = _build_symbols(
        matches,
        source_bytes,
        cfg,
        include_docstrings=include_docstrings,
    )

    filtered = [s for s in symbols if s.kind in wanted]

    truncated = False
    if len(filtered) > SYMBOL_LIMIT:
        filtered = filtered[:SYMBOL_LIMIT]
        truncated = True

    errors = _collect_errors(tree.root_node)
    if truncated:
        errors.append(
            {
                "line": 0,
                "message": (
                    f"truncated to first {SYMBOL_LIMIT} symbols; "
                    f"narrow the request with a tighter `kind` filter."
                ),
            }
        )

    return {
        "language": lang_id,
        "symbols": [s.to_dict(include_docstrings) for s in filtered],
        "errors": errors,
    }


def probe_grammar(
    language: str,
    source: str | bytes,
    *,
    max_depth: int = 12,
    max_nodes: int = 200,
    include_anonymous: bool = False,
) -> str:
    """Return an indented AST dump of `source` parsed as `language`.

    Works for any language the tree-sitter-language-pack ships, not
    just those with a configured .toml/.scm here — that's the point,
    you call this BEFORE you've finished writing the config.

    Each named node prints as `[field_name: ]node_type[  'text']`,
    where text is shown only for token-leaf nodes (identifiers,
    literals, etc.). Anonymous tokens (`(`, `=`, `;`) are hidden by
    default. `max_depth` caps recursion; `max_nodes` caps total lines
    so a probe of a large file stays bounded.
    """
    try:
        lang = _get_language(language)
    except Exception as e:
        return f"<unknown language {language!r}: {e}>"

    from tree_sitter import Parser

    src_bytes = (
        source.encode("utf-8") if isinstance(source, str) else source
    )
    tree = Parser(lang).parse(src_bytes)

    lines: list[str] = []
    state = {"count": 0, "truncated": False}

    def emit(line: str) -> None:
        lines.append(line)
        state["count"] += 1

    def is_token_leaf(node) -> bool:
        # No children, or all children are anonymous tokens.
        return not any(c.is_named for c in node.children)

    def walk(node, depth: int, parent, child_idx: int) -> None:
        if state["truncated"]:
            return
        if state["count"] >= max_nodes:
            state["truncated"] = True
            emit("  " * depth + f"... (truncated at {max_nodes} nodes)")
            return
        if not include_anonymous and not node.is_named:
            return
        field_prefix = ""
        if parent is not None:
            try:
                fname = parent.field_name_for_child(child_idx)
                if fname:
                    field_prefix = f"{fname}: "
            except Exception:
                pass
        text_suffix = ""
        if is_token_leaf(node):
            text = node.text.decode("utf-8", errors="replace")
            text = text[:60].replace("\n", "\\n")
            text_suffix = f"  '{text}'"
        if depth > max_depth:
            emit(
                "  " * depth + f"{field_prefix}{node.type}  ..."
            )
            return
        emit(
            "  " * depth
            + f"{field_prefix}{node.type}{text_suffix}"
        )
        for i, child in enumerate(node.children):
            walk(child, depth + 1, parent=node, child_idx=i)

    walk(tree.root_node, 0, parent=None, child_idx=0)
    return "\n".join(lines)


def map_code_for_path(
    path: str,
    source_bytes: bytes,
    *,
    kind: str = "all",
    include_docstrings: bool = False,
) -> str:
    """Map source bytes attributed to `path`. Dispatches by extension
    and returns a JSON-formatted string suitable for an LLM tool
    response. Unknown extension → `{"error": "unsupported language: …"}`
    (no exception)."""
    _load_registry()
    p = Path(path)
    suffix = p.suffix.lower()
    lang_id = _EXTENSION_TO_LANG.get(suffix)
    if lang_id is None:
        return json.dumps(
            {
                "file": str(path),
                "error": (
                    f"unsupported language for extension {suffix!r}; "
                    f"supported: {list(supported_extensions())}"
                ),
            },
            indent=2,
        )

    body = map_source(
        source_bytes,
        lang_id,
        kind=kind,
        include_docstrings=include_docstrings,
    )
    return json.dumps({"file": str(path), **body}, indent=2)
