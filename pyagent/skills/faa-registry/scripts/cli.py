#!/usr/bin/env python3
"""FAA registry inquiry CLI.

The registry has no JSON API — we GET the public HTML pages and parse
out the `<table class="devkit-table">` blocks the site uses for every
result. Output is markdown-ish so an LLM caller can read it directly.

Subcommands:
    lookup <n_number>            single tail-number record
    search-owner <name>          search by registered owner
    search-make-model <make> [model]   search by manufacturer/model
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlencode

import requests

_BASE = "https://registry.faa.gov/aircraftinquiry"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36 pyagent"
)
_TIMEOUT = 30


def _get(path: str, params: dict[str, str]) -> str:
    url = f"{_BASE}/{path}?{urlencode(params)}"
    try:
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    except requests.RequestException as e:
        return f"<request failed: {e}>"
    if r.status_code != 200:
        return f"<http {r.status_code} from {url}>"
    return r.text


class _TableExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[tuple[list[str], list[list[str]]]] = []
        self._in_target = 0
        self._in_caption = False
        self._in_cell = False
        self._captions: list[str] = []
        self._rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell_buf: list[str] = []
        self._cap_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "table" and "devkit-table" in (attr.get("class") or ""):
            self._in_target += 1
            self._captions = []
            self._rows = []
        if not self._in_target:
            return
        if tag == "caption":
            self._in_caption = True
            self._cap_buf = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []

    def handle_endtag(self, tag: str) -> None:
        if not self._in_target:
            return
        if tag == "caption":
            self._captions.append(_clean("".join(self._cap_buf)))
            self._in_caption = False
        elif tag in ("td", "th"):
            self._row.append(_clean("".join(self._cell_buf)))
            self._in_cell = False
        elif tag == "tr":
            if self._row:
                self._rows.append(self._row)
            self._row = []
        elif tag == "table" and self._in_target:
            self.tables.append((self._captions, self._rows))
            self._in_target -= 1

    def handle_data(self, data: str) -> None:
        if self._in_caption:
            self._cap_buf.append(data)
        elif self._in_cell:
            self._cell_buf.append(data)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _format_tables(html: str) -> str:
    parser = _TableExtractor()
    parser.feed(html)
    if not parser.tables:
        return ""
    out: list[str] = []
    for captions, rows in parser.tables:
        title = " — ".join(c for c in captions if c)
        out.append(f"## {title}" if title else "## (untitled)")
        for row in rows:
            cells = [c for c in row if c]
            if not cells:
                continue
            if len(cells) == 1:
                out.append(cells[0])
            elif len(cells) == 2:
                out.append(f"- {cells[0]}: {cells[1]}")
            elif len(cells) % 2 == 0:
                pairs = zip(cells[0::2], cells[1::2])
                for label, value in pairs:
                    out.append(f"- {label}: {value}")
            else:
                out.append("- " + " | ".join(cells))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def cmd_lookup(args: argparse.Namespace) -> str:
    n = args.n_number.strip().upper().lstrip("N")
    if not n:
        return "<empty n_number>"
    html = _get("Search/NNumberResult", {"nNumberTxt": n})
    if html.startswith("<"):
        return html
    body = _format_tables(html)
    if not body:
        return f"no FAA record for N{n}.\n"
    return body


def cmd_search_owner(args: argparse.Namespace) -> str:
    name = args.name.strip()
    if not name:
        return "<empty name>"
    html = _get("Search/NameResult", {"Mfrtxt": "", "nameTxt": name})
    if html.startswith("<"):
        return html
    body = _format_tables(html)
    if not body:
        return "no matching FAA records.\n"
    if 'class="paginate"' in html or "pagination" in html.lower():
        body += "\n(note: results may span multiple pages; only page 1 shown)\n"
    return body


def cmd_search_make_model(args: argparse.Namespace) -> str:
    make = args.make.strip()
    if not make:
        return "<empty make>"
    params = {"Mfrtxt": make, "Modeltxt": args.model or "", "PageNo": "1"}
    html = _get("Search/MakeModelResult", params)
    if html.startswith("<"):
        return html
    body = _format_tables(html)
    if not body:
        return "no matching FAA records.\n"
    if 'class="paginate"' in html or "pagination" in html.lower():
        body += "\n(note: results may span multiple pages; only page 1 shown)\n"
    return body


def main() -> int:
    ap = argparse.ArgumentParser(prog="faa-registry")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("lookup", help="single tail-number record")
    p.add_argument("n_number", help="N-number, with or without leading N")
    p.set_defaults(func=cmd_lookup)

    p = sub.add_parser("search-owner", help="search by registered owner name")
    p.add_argument("name")
    p.set_defaults(func=cmd_search_owner)

    p = sub.add_parser("search-make-model", help="search by manufacturer/model")
    p.add_argument("make")
    p.add_argument("model", nargs="?", default="")
    p.set_defaults(func=cmd_search_make_model)

    args = ap.parse_args()
    sys.stdout.write(args.func(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
