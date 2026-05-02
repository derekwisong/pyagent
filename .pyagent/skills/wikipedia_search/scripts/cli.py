#!/usr/bin/env python3
"""Wikipedia search CLI backed by en.wikipedia.org.

Public endpoints, no key required. Wikimedia asks for a descriptive
User-Agent. Endpoints documented at
https://www.mediawiki.org/wiki/API:Main_page and
https://en.wikipedia.org/api/rest_v1/.

Subcommands:
    search <query> [--limit N]
    summary <title>
    extract <title> [--full] [--sentences N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from typing import Any

import requests

_API = "https://en.wikipedia.org/w/api.php"
_REST = "https://en.wikipedia.org/api/rest_v1"
_UA = "pyagent-wikipedia-search/0.1 (https://github.com/derekwisong/pyagent)"
_TIMEOUT = 15
_HEADERS = {"User-Agent": _UA, "Accept": "application/json"}

_TAG_RE = re.compile(r"<[^>]+>")


def _get(url: str, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    try:
        r = requests.get(url, params=params, headers=_HEADERS, timeout=_TIMEOUT)
    except requests.RequestException as e:
        return 0, f"<request failed: {e}>"
    if r.status_code == 429:
        return 429, "<rate limited by wikipedia>"
    try:
        return r.status_code, r.json()
    except json.JSONDecodeError:
        return r.status_code, None


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).strip()


def _truncate_sentences(text: str, n: int) -> str:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(parts[:n]).strip()


def cmd_search(args: argparse.Namespace) -> str:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": args.query,
        "srlimit": str(args.limit),
        "format": "json",
        "formatversion": "2",
    }
    status, data = _get(_API, params)
    if status != 200 or not isinstance(data, dict):
        return f"<search failed: status={status}>\n"
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return "<no results>\n"
    out = [
        {
            "title": h.get("title"),
            "pageid": h.get("pageid"),
            "snippet": _strip_tags(h.get("snippet", "")),
        }
        for h in hits
    ]
    return json.dumps(out, indent=2) + "\n"


def cmd_summary(args: argparse.Namespace) -> str:
    title = args.title.strip().replace(" ", "_")
    encoded = urllib.parse.quote(title, safe="")
    url = f"{_REST}/page/summary/{encoded}"
    status, data = _get(url)
    if status == 404:
        return f"<not found: {args.title!r}>\n"
    if status != 200 or not isinstance(data, dict):
        return f"<summary failed: status={status}>\n"
    out = {
        "title": data.get("title"),
        "description": data.get("description"),
        "extract": data.get("extract"),
        "url": data.get("content_urls", {}).get("desktop", {}).get("page"),
        "thumbnail": data.get("thumbnail", {}).get("source"),
        "type": data.get("type"),  # "standard", "disambiguation", etc.
    }
    return json.dumps(out, indent=2) + "\n"


def cmd_extract(args: argparse.Namespace) -> str:
    params: dict[str, Any] = {
        "action": "query",
        "prop": "extracts|info",
        "titles": args.title,
        "explaintext": "1",
        "redirects": "1",
        "inprop": "url",
        "format": "json",
        "formatversion": "2",
    }
    if not args.full:
        params["exintro"] = "1"
    status, data = _get(_API, params)
    if status != 200 or not isinstance(data, dict):
        return f"<extract failed: status={status}>\n"
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return f"<not found: {args.title!r}>\n"
    page = pages[0]
    if page.get("missing"):
        return f"<not found: {args.title!r}>\n"
    text = page.get("extract", "") or ""
    if args.sentences:
        text = _truncate_sentences(text, args.sentences)
    out = {
        "title": page.get("title"),
        "pageid": page.get("pageid"),
        "url": page.get("fullurl"),
        "extract": text,
    }
    return json.dumps(out, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("summary", help="REST summary of a known title")
    s.add_argument("title")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("extract", help="plain-text article extract")
    s.add_argument("title")
    s.add_argument("--full", action="store_true", help="return the whole article")
    s.add_argument("--sentences", type=int, help="truncate to first N sentences")
    s.set_defaults(func=cmd_extract)

    args = p.parse_args(argv)
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
