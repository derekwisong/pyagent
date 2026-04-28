"""Convert a Python function into a JSON-schema tool definition for the LLM.

Type hints become the JSON schema; the docstring becomes the tool description
and per-parameter descriptions.

Example:
    >>> def add(a: int, b: int = 0) -> int:
    ...     '''Add two integers.
    ...
    ...     Args:
    ...         a: The first integer.
    ...         b: The second integer.
    ...     '''
    ...     return a + b
    >>> schema("add", add)
    {
        "name": "add",
        "description": "Add two integers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "The first integer."},
                "b": {"type": "integer", "description": "The second integer."},
            },
            "required": ["a"],
        },
    }
"""

import inspect
import types
from typing import Any, Callable, Union, get_args, get_origin, get_type_hints

from docstring_parser import parse

_JSON_PRIMITIVES = {
    int: "integer",
    float: "number",
    str: "string",
    bool: "boolean",
}


def _type_to_schema(tp: Any) -> dict[str, Any]:
    if tp in _JSON_PRIMITIVES:
        return {"type": _JSON_PRIMITIVES[tp]}

    origin = get_origin(tp)
    if origin is list:
        (item,) = get_args(tp) or (str,)
        return {"type": "array", "items": _type_to_schema(item)}
    if origin is dict:
        return {"type": "object"}
    if origin in (Union, types.UnionType):
        non_none = [a for a in get_args(tp) if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_schema(non_none[0])

    return {"type": "string"}


def schema(name: str, fn: Callable[..., Any]) -> dict[str, Any]:
    """Produce a JSON-schema tool definition for `fn` named `name`."""
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    doc = parse(inspect.getdoc(fn) or "")
    param_descriptions = {p.arg_name: p.description for p in doc.params}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        prop = _type_to_schema(hints.get(pname, str))
        if desc := param_descriptions.get(pname):
            prop["description"] = desc
        properties[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    description = doc.short_description or ""
    if doc.long_description:
        description = f"{description}\n\n{doc.long_description}".strip()
    if doc.returns and doc.returns.description:
        description = (
            f"{description}\n\nReturns: {doc.returns.description}".strip()
        )

    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }
