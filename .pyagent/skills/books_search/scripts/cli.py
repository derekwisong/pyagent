#!/usr/bin/env python3
"""Open Library books search CLI.

Public endpoints, no key required. Docs:
https://openlibrary.org/developers/api

Subcommands:
    search <query> [--limit N]
    isbn <isbn>
    work <key>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

import requests

_BASE = "https://openlibrary.org"
_COVERS = "https://covers.openlibrary.org/b/id"
_UA = "pyagent-books-search/0.1 (https://github.com/derekwisong/pyagent)"
_TIMEOUT = 15
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}


def _get(url: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        return 0, f"<request failed: {e}>"
    if r.status_code == 429:
        return 429, "<rate limited by openlibrary>"
    if r.status_code == 404:
        return 404, None
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, None


def _cover_url(cover_id: int | None) -> str | None:
    return f"{_COVERS}/{cover_id}-M.jpg" if cover_id else None


def _description(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("value")
    return None


def cmd_search(args: argparse.Namespace) -> str:
    params = {
        "q": args.query,
        "limit": str(args.limit),
        "fields": "key,title,author_name,first_publish_year,edition_count,isbn,cover_i",
    }
    status, data = _get(f"{_BASE}/search.json", params)
    if status != 200 or not isinstance(data, dict):
        return f"<search failed: status={status}>\n"
    docs = data.get("docs", [])
    if not docs:
        return "<no results>\n"
    out = []
    for d in docs:
        isbns = d.get("isbn") or []
        out.append(
            {
                "title": d.get("title"),
                "authors": d.get("author_name", []),
                "first_publish_year": d.get("first_publish_year"),
                "work_key": d.get("key"),
                "edition_count": d.get("edition_count"),
                "isbn": isbns[0] if isbns else None,
                "cover_url": _cover_url(d.get("cover_i")),
            }
        )
    return json.dumps(out, indent=2) + "\n"


def cmd_isbn(args: argparse.Namespace) -> str:
    isbn = re.sub(r"[^0-9Xx]", "", args.isbn)
    if len(isbn) not in (10, 13):
        return f"<invalid isbn: {args.isbn!r}>\n"
    params = {
        "bibkeys": f"ISBN:{isbn}",
        "format": "json",
        "jscmd": "data",
    }
    status, data = _get(f"{_BASE}/api/books", params)
    if status != 200 or not isinstance(data, dict):
        return f"<isbn lookup failed: status={status}>\n"
    rec = data.get(f"ISBN:{isbn}")
    if not rec:
        return f"<not found: ISBN {isbn}>\n"
    out = {
        "title": rec.get("title"),
        "authors": [a.get("name") for a in rec.get("authors", [])],
        "publishers": [p.get("name") for p in rec.get("publishers", [])],
        "publish_date": rec.get("publish_date"),
        "pages": rec.get("number_of_pages"),
        "url": rec.get("url"),
        "subjects": [s.get("name") for s in rec.get("subjects", [])],
    }
    return json.dumps(out, indent=2) + "\n"


def cmd_work(args: argparse.Namespace) -> str:
    key = args.key.strip().lstrip("/")
    if not key.startswith("works/"):
        return f"<invalid work key: {args.key!r} (expected /works/OL...W)>\n"
    status, data = _get(f"{_BASE}/{key}.json")
    if status == 404 or data is None:
        return f"<not found: {args.key}>\n"
    if status != 200 or not isinstance(data, dict):
        return f"<work lookup failed: status={status}>\n"
    out = {
        "title": data.get("title"),
        "description": _description(data.get("description")),
        "first_publish_date": data.get("first_publish_date"),
        "subjects": data.get("subjects", []),
        "url": f"{_BASE}/{key}",
    }
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("isbn", help="exact ISBN lookup")
    s.add_argument("isbn")
    s.set_defaults(func=cmd_isbn)

    s = sub.add_parser("work", help="fetch work description and subjects")
    s.add_argument("key", help="work key, e.g. /works/OL27448W")
    s.set_defaults(func=cmd_work)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
