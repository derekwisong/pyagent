"""code-mapper — bundled plugin that exposes a `map_code` tool.

`map_code` runs a tree-sitter parse + tags-style query against a source
file and returns a structured outline (functions, classes, methods,
imports, …). The agent uses this to locate definitions before
read_file'ing precise line ranges, instead of slurping whole files.

v1: Python only. The dispatch table in `mapper.py` is multi-language
ready — adding Rust / C / C++ / HTML is a vendor-the-`.scm` plus a
table entry, not a rewrite.
"""

from __future__ import annotations

from pathlib import Path

from pyagent import permissions
from pyagent.plugins.code_mapper import mapper


def _read_source(path: str) -> tuple[bool, bytes | str]:
    """Read a source file as bytes for tree-sitter.

    Returns (ok, payload). On error, ok=False and payload is a
    leading-`<>`-marker error string the tool returns directly. On
    success, payload is the file's bytes (tree-sitter parses bytes,
    not str — many real C/C++ trees aren't UTF-8 clean).
    """
    if not permissions.require_access(path):
        return (False, f"<permission denied (outside workspace): {path}>")
    p = Path(path)
    try:
        return (True, p.read_bytes())
    except FileNotFoundError:
        return (False, f"<file not found: {path}>")
    except IsADirectoryError:
        return (False, f"<is a directory, not a file: {path}>")
    except PermissionError:
        return (False, f"<permission denied: {path}>")


def register(api):
    def map_code(
        path: str,
        kind: str = "all",
        include_docstrings: bool = False,
    ) -> str:
        """Return a structured outline of a source file's symbols.

        Reach for this before `read_file`'ing a file you don't know
        — gives function/class/import locations with line numbers
        you can pass straight to `read_file(path, start=<line>)`.
        Far cheaper than reading the whole file to find a definition.
        Partial files with syntax errors still produce whatever
        parsed cleanly, with broken regions flagged in `errors`.

        Args:
            path: Source file (must be inside the workspace).
                Supported: .py.
            kind: Symbol categories. One of: "all" (default —
                class/function/method/constant/import), "imports",
                "functions" (incl. methods), "classes", "constants",
                "calls" (noisy — use when chasing a reference).
            include_docstrings: Attach the leading docstring of each
                function/class. Default False.

        Returns:
            JSON: `{file, language, symbols: [{kind, name, line,
            parent}], errors}`. `parent` is the enclosing class for
            methods; null otherwise. Unsupported extension →
            `{file, error: "unsupported language: …"}`.
        """
        ok, payload = _read_source(path)
        if not ok:
            return payload  # type: ignore[return-value]
        return mapper.map_code_for_path(
            path,
            payload,  # type: ignore[arg-type]
            kind=kind,
            include_docstrings=include_docstrings,
        )

    def probe_grammar(
        language: str,
        source: str,
        max_depth: int = 12,
        max_nodes: int = 200,
        include_anonymous: bool = False,
    ) -> str:
        """Print the tree-sitter parse tree for a source snippet.

        Use this when authoring or debugging a code-mapper language
        config (`pyagent/plugins/code_mapper/queries/<lang>.{scm,toml}`).
        Tree-sitter queries reference grammar node types and field
        names by exact string — if your `.scm` says
        `(class_definition name: (identifier) @name)` but the grammar
        actually emits `class_declaration` with no `name:` field, the
        query silently matches nothing. Probing the grammar shows you
        the real shape so you write a correct query the first time.
        See `pyagent/plugins/code_mapper/EXTENDING.md` for the full
        workflow.

        Each named node prints as `[field_name: ]node_type[  'text']`,
        with text shown only for token leaves. Anonymous tokens
        (`(`, `=`, `;`) are hidden unless include_anonymous=True.

        Args:
            language: A tree-sitter-language-pack id ("python",
                "rust", "go", "javascript", etc.). The full list is at
                https://pypi.org/project/tree-sitter-language-pack/.
                Works for languages NOT yet configured here — that's
                the point, you call this before authoring the config.
            source: Source snippet to parse. Keep it small (one or two
                constructs you're trying to query); the output grows
                quickly with file size.
            max_depth: Cap on tree depth. Defaults to 12.
            max_nodes: Cap on emitted lines. Defaults to 200. The
                probe shouldn't need more than a few dozen lines for
                a representative snippet.
            include_anonymous: If True, also show anonymous tokens
                (`(`, `=`, `;`). Default False — these are noise for
                writing queries.

        Returns:
            The indented AST as plain text. Errors (unknown language)
            return a single-line `<...>` marker.
        """
        return mapper.probe_grammar(
            language,
            source,
            max_depth=max_depth,
            max_nodes=max_nodes,
            include_anonymous=include_anonymous,
        )

    api.register_tool("map_code", map_code)
    # probe_grammar is plugin-development infrastructure: dump the
    # tree-sitter parse tree to debug a query. Working agents rarely
    # need it, so keep it out of the root schema. Allowlisted in
    # PYTHON_ENGINEER and SOFTWARE_ENGINEER roles for plugin authors
    # iterating on tree-sitter queries.
    api.register_tool("probe_grammar", probe_grammar, role_only=True)
